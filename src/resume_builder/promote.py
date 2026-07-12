"""Promote a wizard ``BootstrapSession`` into a ``Master`` resume.

The wizard accumulates:

- ``basics`` — name / email / phone / location / links
- ``chunks`` — time-period reflections (some have employment metadata)
- ``employment`` — per-chunk company / role for chunks that map to a job
- ``drafts`` — DraftAccomplishments bucketed into one of the 7 canonical sections
- ``education`` — Education entries (including the Phase-4 status + Phase-4.5 reason)
- ``summary`` — optional one-line headline

``promote_to_master`` collapses that into a ``Master`` pydantic object using
stable, slug-based IDs (``exp-acme-1``, ``proj-dispatchkit-1``, …). The
function never invents content — what isn't in the session shows up as a
warning the caller can surface to the user.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from .schema import (
    Award,
    Basics,
    Bullet,
    Education,
    Experience,
    Master,
    Project,
    SkillGroup,
)
from .session_store import (
    BUCKETS,
    BootstrapSession,
    ChunkEmployment,
    DraftAccomplishment,
    TimeChunk,
)


# Bucket-to-target-section mapping. Drafts whose bucket isn't in this map
# (or that have no bucket at all) get surfaced as a warning and skipped.
_BUCKET_TO_SECTION = {
    "experience": "experience",
    "projects": "projects",
    "education": "education",
    "extracurricular": "extracurricular",
    "skills": "skills",
    "awards": "awards",
    "certifications": "certifications",
}

# Defaults when the user hasn't named their projects / activities / skills.
_DEFAULT_PROJECT_NAME = "Notable Projects"
_DEFAULT_EXTRACURRICULAR_NAME = "Activities"
_DEFAULT_SKILLS_CATEGORY = "Skills"
_DEFAULT_EXPERIENCE_COMPANY = "Independent / Various"
_DEFAULT_EXPERIENCE_ROLE = "Contributor"


@dataclass
class PromoteWarning:
    """One actionable thing the user should know about the promote result."""

    kind: str  # short stable id: "missing_employment" / "no_bucket" / "no_basics" / ...
    message: str
    draft_id: Optional[str] = None
    chunk_id: Optional[str] = None


@dataclass
class PromoteResult:
    master: Master
    warnings: List[PromoteWarning] = field(default_factory=list)


# ---------- public API ----------


def promote_to_master(session: BootstrapSession) -> PromoteResult:
    """Collapse a wizard session into a ``Master``.

    Never raises for shape problems — instead emits warnings on the result.
    The caller (the wizard UI or CLI) decides whether to surface them
    before saving to disk.
    """
    warnings: List[PromoteWarning] = []

    basics = session.basics
    if basics is None or not basics.name.strip():
        warnings.append(PromoteWarning(
            kind="no_basics",
            message="No basics captured yet — fill in your name + contact "
                    "info in Step 7 before saving.",
        ))
        # We still produce a valid Master; pydantic requires Basics.name,
        # so we fall back to a placeholder the user must edit.
        basics = basics or Basics(name="<your name>")
        if not basics.name.strip():
            basics = Basics(
                name="<your name>",
                email=basics.email,
                phone=basics.phone,
                location=basics.location,
                links=basics.links,
            )

    chunks_by_id = {c.id: c for c in session.chunks}
    employment_by_chunk = {e.chunk_id: e for e in session.employment}
    drafts_by_bucket: dict[str, List[DraftAccomplishment]] = {b: [] for b in BUCKETS}

    for draft in session.drafts:
        if not draft.bucket:
            warnings.append(PromoteWarning(
                kind="no_bucket",
                message=(
                    f"Draft has no bucket assigned — categorize it in Step 5 "
                    f"first, or it won't make it into the master."
                ),
                draft_id=draft.id,
            ))
            continue
        if draft.bucket not in _BUCKET_TO_SECTION:
            warnings.append(PromoteWarning(
                kind="unknown_bucket",
                message=f"Draft has unknown bucket {draft.bucket!r}; skipped.",
                draft_id=draft.id,
            ))
            continue
        drafts_by_bucket[draft.bucket].append(draft)

    experience = _build_experience(
        drafts_by_bucket["experience"],
        chunks_by_id,
        employment_by_chunk,
        warnings,
    )
    projects = _build_projects(drafts_by_bucket["projects"])
    extracurricular = _build_extracurricular(drafts_by_bucket["extracurricular"])
    skills = _build_skills(drafts_by_bucket["skills"])
    awards = _build_awards(drafts_by_bucket["awards"], warnings)
    education = _build_education(
        list(session.education),
        drafts_by_bucket["education"],
        drafts_by_bucket["certifications"],
        warnings,
    )

    master = Master(
        basics=basics,
        summary=(session.summary or "").strip() or None,
        experience=experience,
        projects=projects,
        education=education,
        skills=skills,
        awards=awards,
        extracurricular=extracurricular,
    )
    return PromoteResult(master=master, warnings=warnings)


# ---------- helpers ----------


def _build_experience(
    drafts: List[DraftAccomplishment],
    chunks_by_id: dict,
    employment_by_chunk: dict,
    warnings: List[PromoteWarning],
) -> List[Experience]:
    """One ``Experience`` per chunk that has experience-bucketed drafts.

    Chunks without employment metadata still produce an Experience — but
    with placeholder company/role and a warning telling the user to fill
    Step 8 (or edit the YAML before saving).
    """
    by_chunk: dict[str, List[DraftAccomplishment]] = {}
    for d in drafts:
        by_chunk.setdefault(d.chunk_id, []).append(d)

    out: List[Experience] = []
    for chunk_id, chunk_drafts in by_chunk.items():
        chunk: Optional[TimeChunk] = chunks_by_id.get(chunk_id)
        emp: Optional[ChunkEmployment] = employment_by_chunk.get(chunk_id)
        company = (emp.company.strip() if emp and emp.company else "")
        role = (emp.role.strip() if emp and emp.role else "")
        if not company or not role:
            warnings.append(PromoteWarning(
                kind="missing_employment",
                message=(
                    f"Chunk {chunk.label if chunk else chunk_id!r} has "
                    f"experience drafts but no company/role set. Add them "
                    f"in Step 8 — meanwhile we're parking these bullets "
                    f"under '{_DEFAULT_EXPERIENCE_COMPANY}'."
                ),
                chunk_id=chunk_id,
            ))
            company = company or _DEFAULT_EXPERIENCE_COMPANY
            role = role or _DEFAULT_EXPERIENCE_ROLE

        start = (emp.start_override if emp and emp.start_override
                 else (chunk.start if chunk else ""))
        end = (emp.end_override if emp and emp.end_override
               else (chunk.end if chunk else ""))
        location = emp.location if emp else None

        slug = _slugify(company)
        exp_id = f"exp-{slug}-{len(out) + 1}"
        bullets = [_to_bullet(d, exp_id, i + 1) for i, d in enumerate(chunk_drafts)]
        out.append(Experience(
            id=exp_id,
            company=company,
            role=role,
            start=start,
            end=end,
            location=location,
            bullets=bullets,
        ))
    # Order chronologically — most recent first by start date string compare
    # (works for "YYYY-MM" format).
    out.sort(key=lambda e: e.start, reverse=True)
    return out


def _build_projects(drafts: List[DraftAccomplishment]) -> List[Project]:
    """All project drafts land under a single default Project for v1.

    The user can split into named projects by editing the YAML in the
    Save Master step's preview textarea. A future phase could ask
    per-draft for a project name; for now this keeps the flow simple.
    """
    if not drafts:
        return []
    proj_id = f"proj-{_slugify(_DEFAULT_PROJECT_NAME)}-1"
    bullets = [_to_bullet(d, proj_id, i + 1) for i, d in enumerate(drafts)]
    return [Project(id=proj_id, name=_DEFAULT_PROJECT_NAME, bullets=bullets)]


def _build_extracurricular(drafts: List[DraftAccomplishment]) -> List[Project]:
    if not drafts:
        return []
    activity_id = f"extra-{_slugify(_DEFAULT_EXTRACURRICULAR_NAME)}-1"
    bullets = [_to_bullet(d, activity_id, i + 1) for i, d in enumerate(drafts)]
    return [Project(id=activity_id, name=_DEFAULT_EXTRACURRICULAR_NAME, bullets=bullets)]


def _build_skills(drafts: List[DraftAccomplishment]) -> List[SkillGroup]:
    """One default SkillGroup with each draft's bullet text as one item.

    Skills drafts that are full bullets (e.g. "Fluent in Python and Go")
    end up as items. The user can split into categories in the YAML editor.
    """
    if not drafts:
        return []
    items = [d.draft_bullet.strip() for d in drafts if d.draft_bullet.strip()]
    return [SkillGroup(category=_DEFAULT_SKILLS_CATEGORY, items=items)]


def _build_awards(
    drafts: List[DraftAccomplishment],
    warnings: List[PromoteWarning],
) -> List[Award]:
    """Awards-bucket drafts become standalone Award entries.

    Draft text becomes the award name; raw_quote becomes the criteria
    (since that's the user's source-of-truth context). User can refine
    in the YAML editor.
    """
    out: List[Award] = []
    for d in drafts:
        out.append(Award(
            name=d.draft_bullet.strip(),
            criteria=d.raw_quote.strip() or None,
        ))
        if not d.raw_quote.strip():
            warnings.append(PromoteWarning(
                kind="award_no_criteria",
                message=(
                    f"Award has no criteria — per Bock, 'a trophy without "
                    f"context is noise.' Edit the YAML to add criteria."
                ),
                draft_id=d.id,
            ))
    return out


def _build_education(
    base_education: List[Education],
    education_drafts: List[DraftAccomplishment],
    certification_drafts: List[DraftAccomplishment],
    warnings: List[PromoteWarning],
) -> List[Education]:
    """Combine the user's Step-6 Education entries with any
    education/certification-bucketed drafts.

    Education-bucketed drafts attach to the first Education entry's
    ``notes`` field (a heuristic — user can split). Certification-bucketed
    drafts emit one Education entry each with ``status=certification_only``,
    using the draft text as the degree name.
    """
    out: List[Education] = []
    for edu in base_education:
        out.append(edu.model_copy(deep=True))

    # Attach education-bucketed drafts as notes on the most recent entry.
    if education_drafts:
        if not out:
            warnings.append(PromoteWarning(
                kind="education_drafts_orphaned",
                message=(
                    f"Found {len(education_drafts)} education-bucketed draft(s) "
                    f"but no Education entries in Step 6. Add at least one "
                    f"degree first, or edit the YAML to attach them manually."
                ),
            ))
        else:
            existing_notes = out[0].notes or ""
            extra = "\n".join(f"• {d.draft_bullet.strip()}" for d in education_drafts)
            out[0].notes = (existing_notes + "\n" + extra).strip() if existing_notes else extra

    # Certifications become their own Education entries.
    for i, d in enumerate(certification_drafts, start=1):
        text = d.draft_bullet.strip()
        out.append(Education(
            id=f"cert-{_slugify(text[:24])}-{i}",
            school=d.tags_hint[0] if d.tags_hint else "—",
            degree=text,
            year="",
            status="certification_only",
        ))

    return out


def _to_bullet(draft: DraftAccomplishment, container_id: str, idx: int) -> Bullet:
    """Convert a DraftAccomplishment to a master ``Bullet``.

    Stable id format: ``<container-id>-bullet-<n>``. Tag hints carry
    through; impact_score_hint becomes impact_score; raw_quote + every
    user_followup land in ``variants`` so the no-invention guard treats
    them as legal source vocabulary at tailor time.
    """
    return Bullet(
        id=f"{container_id}-bullet-{idx}",
        text=draft.draft_bullet.strip(),
        tags=list(draft.tags_hint),
        impact_score=draft.impact_score_hint,
        variants=[draft.raw_quote.strip(), *(s.strip() for s in draft.user_followups if s.strip())],
    )


# ---------- slug helper ----------


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str) -> str:
    """Produce a stable, filename-safe slug.

    Strips diacritics best-effort, lowercases, collapses runs of
    non-alphanumeric chars to a single hyphen, trims leading/trailing
    hyphens. Falls back to ``"untitled"`` on empty input.
    """
    if not text:
        return "untitled"
    # Best-effort diacritic strip via NFKD normalisation.
    import unicodedata
    normalised = unicodedata.normalize("NFKD", text)
    ascii_only = "".join(c for c in normalised if not unicodedata.combining(c))
    slug = _SLUG_RE.sub("-", ascii_only.lower()).strip("-")
    return slug or "untitled"
