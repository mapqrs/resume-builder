"""Résumé import: parse an existing resume into a pre-filled wizard session.

One LLM call turns pasted/uploaded resume text into structured JSON
(basics, employment history, education, skills, summary). ``apply_import``
then maps that onto a ``BootstrapSession``:

- one ``TimeChunk`` per employment (labelled "Company — Role"), seeded with
  the resume's own bullet text as ``raw_notes`` — the user reviews and runs
  the normal Extract step per chunk, so the anti-fabrication pipeline is
  identical to a hand-typed brain dump;
- ``ChunkEmployment`` metadata (company / role / location) pre-filled;
- basics / education / summary applied directly;
- each skill becomes one skills-bucket draft (promote turns those into
  ``SkillGroup`` items).

The parser transcribes; it must not embellish. Everything it produces is
reviewed by the user before promote, and the tailor's no-invention guard
still runs on every generated output downstream.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import List, Optional

from pydantic import BaseModel, Field

from .promote import _slugify
from .schema import Basics, Education, Link
from .session_store import (
    BootstrapSession,
    ChunkEmployment,
    DraftAccomplishment,
    TimeChunk,
)


MIN_IMPORT_CHARS = 200  # anything shorter is not a resume


class ImportParseError(RuntimeError):
    """The LLM reply could not be parsed into a usable resume structure."""


IMPORT_SYSTEM_PROMPT = """You are a resume PARSER. You receive the raw text of a person's existing resume and return it as structured JSON.

HARD RULES:
1. TRANSCRIBE, never embellish. Copy the candidate's wording for bullets verbatim (minus layout junk like page numbers or repeated headers). Do not add, merge, reorder, or "improve" content.
2. NEVER invent values. A field you cannot find in the text is null (or [] for lists).
3. Dates are "YYYY-MM". If only a year is given, use "YYYY-01" for starts and "YYYY-12" for ends. A current position ("Present", "till date") has end null.
4. Employment order: most recent first, exactly as resumes usually list them.
5. education[].status is one of: graduated, in_progress, dropout, deferred_admit, rejected_admit, on_leave, certification_only, online_only. Default to "graduated" when the text doesn't say otherwise.
6. skills is a flat list of individual skills ("Python", "Kubernetes"), split from any comma/pipe-separated skill lines. No sentence-form entries.
7. Certifications (AWS SA, PMP, etc.) are education entries with status "certification_only".
8. warnings: one short string per thing you saw but could not place (ambiguous dates, unlabeled sections, truncated text).

OUTPUT: ONLY valid JSON matching the schema below. No prose, no markdown fences."""


IMPORT_JSON_SCHEMA_HINT = """{
  "basics": {
    "name": "<full name or null>",
    "email": "<email or null>",
    "phone": "<phone or null>",
    "location": "<city/region or null>",
    "links": [{"label": "GitHub", "url": "https://..."}]
  },
  "summary": "<the resume's own summary/objective text, or null>",
  "employment": [
    {
      "company": "<employer>",
      "role": "<job title>",
      "start": "YYYY-MM or null",
      "end": "YYYY-MM or null (null = current)",
      "location": "<location or null>",
      "bullets": ["<verbatim bullet 1>", "<verbatim bullet 2>"]
    }
  ],
  "education": [
    {
      "school": "<institution>",
      "degree": "<degree/program>",
      "year": "<completion year or null>",
      "status": "graduated",
      "location": "<location or null>",
      "gpa": "<GPA/percentage as written, or null>"
    }
  ],
  "skills": ["Python", "Kubernetes"],
  "warnings": ["<anything you could not place>"]
}"""


def build_import_user_message(resume_text: str) -> str:
    return f"""# Resume text (parse this)

```
{resume_text.strip()}
```

# Output schema (return ONLY this JSON, no prose, no fences)

{IMPORT_JSON_SCHEMA_HINT}
"""


# ---------- parsed-resume models (lenient by design) ----------


class ImportedLink(BaseModel):
    label: str = ""
    url: str = ""


class ImportedBasics(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    links: List[ImportedLink] = Field(default_factory=list)


class ImportedEmployment(BaseModel):
    company: str = ""
    role: str = ""
    start: Optional[str] = None  # "YYYY-MM"
    end: Optional[str] = None  # None = current
    location: Optional[str] = None
    bullets: List[str] = Field(default_factory=list)


class ImportedEducation(BaseModel):
    school: str = ""
    degree: str = ""
    year: Optional[str] = None
    status: str = "graduated"
    location: Optional[str] = None
    gpa: Optional[str] = None


class ParsedResume(BaseModel):
    basics: Optional[ImportedBasics] = None
    summary: Optional[str] = None
    employment: List[ImportedEmployment] = Field(default_factory=list)
    education: List[ImportedEducation] = Field(default_factory=list)
    skills: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


def parse_import_response(raw: str) -> ParsedResume:
    """Parse the LLM's reply. Tolerates code fences and surrounding prose."""
    text = (raw or "").strip()
    fence_match = re.match(r"^```(?:json)?\s*\n(.*)\n```$", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()
    if not text.startswith("{"):
        start = text.find("{")
        if start == -1:
            raise ImportParseError("LLM response did not contain a JSON object")
        text = text[start:]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as e:
        raise ImportParseError(f"import parse: JSON decode failed: {e}") from e
    if not isinstance(payload, dict):
        raise ImportParseError("import parse: response must be a JSON object")
    try:
        return ParsedResume.model_validate(payload)
    except Exception as e:  # pydantic ValidationError
        raise ImportParseError(f"import parse: unexpected shape: {e}") from e


# ---------- applying a parse to a session ----------


_YM_RE = re.compile(r"^(\d{4})-(\d{2})$")
_YEAR_RE = re.compile(r"^(\d{4})$")


def _norm_ym(value: Optional[str], *, year_fallback_month: str) -> Optional[str]:
    """Normalise a date to "YYYY-MM". Bare years get the fallback month;
    anything unparseable becomes None (never a guess)."""
    if not value:
        return None
    value = str(value).strip()
    m = _YM_RE.match(value)
    if m:
        month = int(m.group(2))
        if 1 <= month <= 12:
            return value
        return None
    y = _YEAR_RE.match(value)
    if y:
        return f"{y.group(1)}-{year_fallback_month}"
    return None


def _now_ym() -> str:
    now = datetime.now(timezone.utc)
    return f"{now.year}-{now.month:02d}"


class ImportSummary(BaseModel):
    """What the import filled in — the UI renders this as review chips."""

    employment_chunks: int = 0
    education_entries: int = 0
    skills: int = 0
    basics_filled: bool = False
    summary_filled: bool = False
    career_start: Optional[str] = None
    warnings: List[str] = Field(default_factory=list)


def session_has_content(session: BootstrapSession) -> bool:
    """True when an import would overwrite something the user typed."""
    return bool(
        any((c.raw_notes or "").strip() for c in session.chunks)
        or session.drafts
        or session.education
        or session.basics is not None
    )


def apply_import(session: BootstrapSession, parsed: ParsedResume) -> ImportSummary:
    """Map a parsed resume onto the session, replacing chunk/draft state.

    Employment becomes one chunk per job (ascending by start date) with the
    resume's own bullets as raw_notes. Jobs without a parseable start date
    are reported in warnings rather than silently guessed.
    """
    summary = ImportSummary(warnings=list(parsed.warnings))

    # --- employment → chunks + ChunkEmployment ---
    dated: List[tuple[str, str, ImportedEmployment]] = []
    for emp in parsed.employment:
        start = _norm_ym(emp.start, year_fallback_month="01")
        if start is None:
            label = " — ".join(x for x in (emp.company, emp.role) if x) or "one position"
            summary.warnings.append(
                f"Couldn't read a start date for {label}; add that job manually."
            )
            continue
        end = _norm_ym(emp.end, year_fallback_month="12") or _now_ym()
        dated.append((start, end, emp))
    dated.sort(key=lambda t: t[0])

    chunks: List[TimeChunk] = []
    employment: List[ChunkEmployment] = []
    seen_ids: dict[str, int] = {}
    for start, end, emp in dated:
        base_id = f"chunk-{start}"
        seen_ids[base_id] = seen_ids.get(base_id, 0) + 1
        chunk_id = base_id if seen_ids[base_id] == 1 else f"{base_id}-{seen_ids[base_id]}"
        label = " — ".join(x for x in (emp.company, emp.role) if x) or start
        chunks.append(
            TimeChunk(
                id=chunk_id,
                label=label,
                start=start,
                end=end,
                raw_notes="\n".join(b.strip() for b in emp.bullets if b.strip()),
            )
        )
        employment.append(
            ChunkEmployment(
                chunk_id=chunk_id,
                company=emp.company,
                role=emp.role,
                location=emp.location,
            )
        )

    session.chunks = chunks
    session.employment = employment
    session.drafts = []  # old drafts reference old chunk ids
    summary.employment_chunks = len(chunks)
    if chunks:
        session.career_start = chunks[0].start
        summary.career_start = chunks[0].start

    # --- skills → one skills-bucket draft per skill ---
    skills = [s.strip() for s in parsed.skills if s and s.strip()]
    if skills and chunks:
        anchor = chunks[-1].id  # most recent job
        source = ", ".join(skills)
        for i, skill in enumerate(skills, start=1):
            session.drafts.append(
                DraftAccomplishment(
                    id=f"import-skill-{i}",
                    chunk_id=anchor,
                    raw_quote=source,
                    draft_bullet=skill,
                    bucket="skills",
                )
            )
        summary.skills = len(skills)
    elif skills:
        summary.warnings.append(
            "Found a skills list but no dated employment to attach it to; "
            "add skills during categorization instead."
        )

    # --- basics ---
    if parsed.basics and (parsed.basics.name or "").strip():
        session.basics = Basics(
            name=parsed.basics.name.strip(),
            email=(parsed.basics.email or "").strip() or None,
            phone=(parsed.basics.phone or "").strip() or None,
            location=(parsed.basics.location or "").strip() or None,
            links=[
                Link(label=lk.label.strip() or "Link", url=lk.url.strip())
                for lk in parsed.basics.links
                if lk.url.strip()
            ],
        )
        summary.basics_filled = True
    elif parsed.basics:
        summary.warnings.append("No name found — fill in Basics manually.")

    # --- summary ---
    if (parsed.summary or "").strip():
        session.summary = parsed.summary.strip()
        summary.summary_filled = True

    # --- education ---
    edu_entries: List[Education] = []
    seen_slugs: dict[str, int] = {}
    for imp in parsed.education:
        if not imp.school.strip() and not imp.degree.strip():
            continue
        slug = _slugify(imp.school or imp.degree)
        seen_slugs[slug] = seen_slugs.get(slug, 0) + 1
        eid = f"edu-{slug}" if seen_slugs[slug] == 1 else f"edu-{slug}-{seen_slugs[slug]}"
        status = imp.status if imp.status in (
            "graduated", "in_progress", "dropout", "deferred_admit",
            "rejected_admit", "on_leave", "certification_only", "online_only",
        ) else "graduated"
        edu_entries.append(
            Education(
                id=eid,
                school=imp.school.strip(),
                degree=imp.degree.strip(),
                year=(imp.year or "").strip(),
                status=status,
                location=(imp.location or "").strip() or None,
                gpa=(imp.gpa or "").strip() or None,
            )
        )
    session.education = edu_entries
    summary.education_entries = len(edu_entries)

    return summary
