"""Persistence for wizard / bootstrap sessions.

Each session lives in ``<sessions_dir>/<uuid>/state.yaml``. Writes are
atomic (temp file + rename) so a crash mid-write never corrupts state.

The session captures everything the wizard accumulates: time chunks,
raw-text dumps per chunk, extracted drafts, categorized buckets,
education entries, and a target-role payload. Most fields start empty
and fill in as the wizard advances — Phase 0 only needs the skeleton.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Literal, Optional

import yaml
from pydantic import BaseModel, Field

from .schema import Basics, Education, TargetRole


DEFAULT_SESSIONS_DIR = Path("sessions")
STATE_FILENAME = "state.yaml"

# The 7 fixed buckets used during categorization (Phase 3). Kept as a
# constant so the wizard UI and the categorizer share one source of truth.
BUCKETS = (
    "experience",
    "projects",
    "education",
    "extracurricular",
    "skills",
    "awards",
    "certifications",
)
Bucket = Literal[
    "experience",
    "projects",
    "education",
    "extracurricular",
    "skills",
    "awards",
    "certifications",
]


# ---------- nested models ----------


class TimeChunk(BaseModel):
    """One slice of the user's career to reflect on."""

    id: str  # stable, e.g. "chunk-2023-h1"
    label: str  # display, e.g. "H1 2023"
    start: str  # "YYYY-MM"
    end: str  # "YYYY-MM"
    raw_notes: str = ""


class DraftAccomplishment(BaseModel):
    """One LLM-extracted accomplishment, with provenance back to raw notes."""

    id: str
    chunk_id: str
    raw_quote: str  # substring of TimeChunk.raw_notes that grounds this
    draft_bullet: str  # may contain [NUMBER], [METHOD], [TIMEFRAME] placeholders
    tier: Literal["original", "better", "awesome"] = "original"
    missing: List[
        Literal["y_metric", "z_method", "x_strong_verb"]
    ] = Field(default_factory=list)
    impact_score_hint: Optional[int] = Field(default=None, ge=1, le=5)
    tags_hint: List[str] = Field(default_factory=list)
    bucket: Optional[Bucket] = None
    user_confirmed: bool = False
    # When ``y_metric`` is missing, the wizard suggests where to look
    # (perf reviews, OKRs, Slack threads, etc.). Phase 5 populates this.
    where_to_look: List[str] = Field(default_factory=list)
    # Free-text follow-ups supplied by the user during the polish phase.
    # The anti-fabrication guard treats these as legal source vocabulary.
    user_followups: List[str] = Field(default_factory=list)


# ---------- top-level session ----------


class ChunkEmployment(BaseModel):
    """Per-chunk employment metadata captured during the Save Master step.

    Each chunk that has experience-bucketed drafts maps to one ``Experience``
    in the final master. The wizard collects the company/role/location here
    in Phase 6 — chunk dates are inherited from the ``TimeChunk`` itself.
    """

    chunk_id: str
    company: str = ""
    role: str = ""
    location: Optional[str] = None
    # Override the chunk's start/end if the employment spanned a different
    # window than the reflection chunk. Both optional — defaults fall back
    # to TimeChunk.start / TimeChunk.end.
    start_override: Optional[str] = None
    end_override: Optional[str] = None


class BootstrapSession(BaseModel):
    id: str
    created_at: str  # ISO 8601 UTC
    updated_at: str  # ISO 8601 UTC
    # Role family id from role_families.ROLE_FAMILIES, or None until picked.
    # When ``other``, ``role_family_other`` carries the user's free-text label.
    role_family: Optional[str] = None
    role_family_other: Optional[str] = None
    career_start: Optional[str] = None  # "YYYY-MM"
    # Phase 10 item 2: explicit cadence override. None means auto-pick via
    # chunk_size_months. Valid values: "monthly" | "quarterly" |
    # "six-monthly" | "annual".
    cadence: Optional[str] = None
    chunks: List[TimeChunk] = Field(default_factory=list)
    drafts: List[DraftAccomplishment] = Field(default_factory=list)
    education: List[Education] = Field(default_factory=list)
    target_role: Optional[TargetRole] = None
    # Phase 6 — basics + per-chunk employment for the promote-to-master step.
    basics: Optional[Basics] = None
    employment: List[ChunkEmployment] = Field(default_factory=list)
    summary: Optional[str] = None
    promoted_master_path: Optional[str] = None
    notes: Optional[str] = None  # free-form scratch / debugging

    def touch(self) -> None:
        """Update ``updated_at`` to now (UTC ISO 8601)."""
        self.updated_at = _now_iso()


# ---------- store API ----------


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_session() -> BootstrapSession:
    """Create a fresh session with a new uuid + current timestamps."""
    now = _now_iso()
    return BootstrapSession(
        id=uuid.uuid4().hex[:12],
        created_at=now,
        updated_at=now,
    )


def session_dir(session_id: str, sessions_root: Path = DEFAULT_SESSIONS_DIR) -> Path:
    return Path(sessions_root) / session_id


def save(
    session: BootstrapSession,
    sessions_root: Path = DEFAULT_SESSIONS_DIR,
) -> Path:
    """Atomically write the session to ``<root>/<id>/state.yaml``.

    Uses ``os.replace`` for an atomic swap on the same filesystem so a
    crash mid-write leaves the previous file intact.
    """
    session.touch()
    target_dir = session_dir(session.id, sessions_root)
    target_dir.mkdir(parents=True, exist_ok=True)
    final = target_dir / STATE_FILENAME
    tmp = target_dir / (STATE_FILENAME + ".tmp")

    payload = session.model_dump(mode="json")
    tmp.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    os.replace(tmp, final)
    return final


def load(
    session_id: str,
    sessions_root: Path = DEFAULT_SESSIONS_DIR,
) -> BootstrapSession:
    """Load a session by id. Raises ``FileNotFoundError`` if missing."""
    path = session_dir(session_id, sessions_root) / STATE_FILENAME
    if not path.exists():
        raise FileNotFoundError(f"No session at {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return BootstrapSession.model_validate(data or {})


def list_sessions(sessions_root: Path = DEFAULT_SESSIONS_DIR) -> List[str]:
    """Return all session ids found under ``sessions_root``, newest first.

    Ordering is by ``updated_at`` from each state file; sessions whose
    state file fails to load are skipped silently.
    """
    root = Path(sessions_root)
    if not root.exists():
        return []
    sessions: List[tuple[str, str]] = []
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        state = entry / STATE_FILENAME
        if not state.exists():
            continue
        try:
            data = yaml.safe_load(state.read_text(encoding="utf-8")) or {}
            sessions.append((entry.name, data.get("updated_at", "")))
        except (OSError, yaml.YAMLError):
            continue
    sessions.sort(key=lambda s: s[1], reverse=True)
    return [sid for sid, _ in sessions]


def delete_session(
    session_id: str,
    sessions_root: Path = DEFAULT_SESSIONS_DIR,
) -> bool:
    """Remove a session directory entirely. Returns True if anything was deleted."""
    target = session_dir(session_id, sessions_root)
    if not target.exists():
        return False
    # Walk + remove. Defensive: only touch files we expect.
    for child in target.iterdir():
        if child.is_file():
            child.unlink()
    target.rmdir()
    return True


# ---------- chunk math (used by the wizard, Phase 1 + Phase 10 item 2) ----------


# Named cadences the wizard exposes (Phase 10 item 2). Each carries months,
# label, and human-readable guidance about when to pick it.
CADENCE_MONTHS: dict[str, int] = {
    "monthly": 1,
    "quarterly": 3,
    "six-monthly": 6,
    "annual": 12,
}

CADENCE_GUIDANCE: dict[str, dict[str, str]] = {
    "monthly": {
        "label": "Monthly",
        "best_for": "Past 12-18 months only",
        "notes": (
            "Highest memory resolution. Use this for your current job + "
            "the one just before. Too granular for older periods — you'll "
            "spend more time on labels than dumping."
        ),
    },
    "quarterly": {
        "label": "Quarterly",
        "best_for": "Past 2-3 years",
        "notes": (
            "Sweet spot for recent past. One quarter ≈ one project cycle "
            "in most teams, so each chunk maps to a real shipped thing."
        ),
    },
    "six-monthly": {
        "label": "Six-monthly",
        "best_for": "Mid-career (3-10 years)",
        "notes": (
            "The default. Coarse enough to feel finishable, fine enough "
            "to surface distinct accomplishments per chunk."
        ),
    },
    "annual": {
        "label": "Annual",
        "best_for": "Distant past (10+ years ago)",
        "notes": (
            "Memory has decayed; you only remember a few highlights "
            "anyway. Keep it light — one chunk per year, 2-3 bullets each."
        ),
    },
}


def chunk_size_months(career_start: str, today: Optional[datetime] = None) -> int:
    """Auto-pick a default cadence: 6 months if career <5 years, else 12.

    ``career_start`` is "YYYY-MM"; ``today`` defaults to now (UTC). This
    is the fallback when the user hasn't picked a cadence explicitly via
    ``cadence=`` in ``default_chunks_for``.
    """
    if today is None:
        today = datetime.now(timezone.utc)
    try:
        year, month = career_start.split("-")
        start_dt = datetime(int(year), int(month), 1, tzinfo=timezone.utc)
    except (ValueError, AttributeError) as e:
        raise ValueError(
            f"career_start must be 'YYYY-MM', got {career_start!r}"
        ) from e
    months = (today.year - start_dt.year) * 12 + (today.month - start_dt.month)
    return 6 if months < 60 else 12


def suggest_cadence(career_start: str, today: Optional[datetime] = None) -> str:
    """Return the cadence key the wizard pre-selects for a fresh user.

    Heuristic:
      < 2 years  → quarterly  (fresh memory; one cycle per chunk)
      2-10 years → six-monthly (the previous default)
      10+ years  → annual     (memory decay; keep it light)

    Users can always override in the picker.
    """
    if today is None:
        today = datetime.now(timezone.utc)
    try:
        year, month = career_start.split("-")
        start_dt = datetime(int(year), int(month), 1, tzinfo=timezone.utc)
    except (ValueError, AttributeError) as e:
        raise ValueError(
            f"career_start must be 'YYYY-MM', got {career_start!r}"
        ) from e
    months = (today.year - start_dt.year) * 12 + (today.month - start_dt.month)
    if months < 24:
        return "quarterly"
    if months < 120:
        return "six-monthly"
    return "annual"


def default_chunks_for(
    career_start: str,
    today: Optional[datetime] = None,
    cadence: Optional[str] = None,
) -> List[TimeChunk]:
    """Generate the chunk list for a given career_start.

    When ``cadence`` is one of ``CADENCE_MONTHS`` (monthly / quarterly /
    six-monthly / annual), the corresponding chunk size is used. Otherwise
    falls back to the auto-pick from ``chunk_size_months`` (6 or 12).

    The final chunk is truncated to end at ``today``. Labels are
    human-readable per the cadence: ``"2024-03"`` monthly, ``"Q1 2024"``
    quarterly, ``"H1 2024"`` six-monthly, ``"2024"`` annual.
    """
    if today is None:
        today = datetime.now(timezone.utc)

    if cadence is not None:
        if cadence not in CADENCE_MONTHS:
            raise ValueError(
                f"unknown cadence {cadence!r}; "
                f"valid: {sorted(CADENCE_MONTHS)}"
            )
        size = CADENCE_MONTHS[cadence]
    else:
        size = chunk_size_months(career_start, today)

    try:
        year, month = career_start.split("-")
        cursor = datetime(int(year), int(month), 1, tzinfo=timezone.utc)
    except (ValueError, AttributeError) as e:
        raise ValueError(
            f"career_start must be 'YYYY-MM', got {career_start!r}"
        ) from e

    chunks: List[TimeChunk] = []
    while cursor < today:
        next_month_zero_based = cursor.month - 1 + size
        next_year = cursor.year + next_month_zero_based // 12
        next_month = next_month_zero_based % 12 + 1
        end_dt = datetime(next_year, next_month, 1, tzinfo=timezone.utc)
        # Clamp the final chunk to today.
        if end_dt > today:
            end_dt = today

        label = _label_for(cursor, size)
        chunks.append(
            TimeChunk(
                id=f"chunk-{cursor.year}-{cursor.month:02d}",
                label=label,
                start=f"{cursor.year}-{cursor.month:02d}",
                end=f"{end_dt.year}-{end_dt.month:02d}",
            )
        )
        cursor = datetime(next_year, next_month, 1, tzinfo=timezone.utc)
    return chunks


def _label_for(start: datetime, size_months: int) -> str:
    if size_months == 1:
        return f"{start.year}-{start.month:02d}"
    if size_months == 3:
        quarter = (start.month - 1) // 3 + 1
        return f"Q{quarter} {start.year}"
    if size_months == 6:
        half = "H1" if start.month <= 6 else "H2"
        return f"{half} {start.year}"
    if size_months == 12:
        return str(start.year)
    return f"{start.year}-{start.month:02d} +{size_months}mo"
