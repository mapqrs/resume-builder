"""Applications tracker: a lightweight log of every résumé you generate.

Each generation (auto or copy-paste render) appends one record to
``applications.json`` in the working directory — a derived JD label + snippet,
the ATS coverage score, the per-run pointers, and a timestamp — so the tool
becomes a job-search companion you return to per application, not a one-shot
generator.

Metadata only: no résumé files are stored (the web UI streams the ``.docx`` to
your browser). The log lives next to ``master.yaml`` / ``pointers.yaml`` and is
wiped by the Delete-my-data button.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

from .ats import ATSReport
from .jd_signals import JDSignals
from .schema import Pointers


DEFAULT_PATH = Path("applications.json")
MAX_RECORDS = 500  # keep the file bounded; oldest fall off

# Where an application is in the user's pipeline. "saved" = just generated,
# not yet applied. The rest track the job-search funnel.
STATUSES = ("saved", "applied", "interviewing", "offer", "rejected")


class Application(BaseModel):
    """One generation event."""

    id: str
    created_at: str  # ISO 8601 UTC
    kind: str = "resume"
    label: str = "Untitled application"
    jd_snippet: str = ""
    from_target_role: bool = False  # True when generated in JD-less mode
    ats_score: Optional[float] = None  # 0.0-1.0
    ats_matched: int = 0
    ats_total: int = 0
    length: Optional[str] = None
    seniority: Optional[str] = None
    context: Optional[str] = None
    guard_dropped: int = 0
    status: str = "saved"

    @field_validator("status", mode="before")
    @classmethod
    def _clean_status(cls, v):
        # Tolerate legacy/blank/unknown values so an old file never fails to load.
        return v if v in STATUSES else "saved"


def _now_iso() -> str:
    # Microsecond precision so records logged in the same second still sort
    # deterministically (lexicographic == chronological with fixed-width %f).
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _derive_label(signals: Optional[JDSignals], jd_text: str) -> str:
    """A short human label for the application card.

    Prefers the JD's extracted title, then seniority + role archetype, then the
    first meaningful line of the JD. Never invents — it's all observed input.
    """
    if signals is not None:
        if signals.title:
            return _collapse_ws(signals.title)[:80]
        parts = [p for p in (signals.inferred_seniority, signals.role_archetype) if p]
        if parts:
            return " ".join(parts).replace("-", " ").title()[:80]
    for line in (jd_text or "").splitlines():
        line = line.strip()
        if len(line) >= 3:
            return line[:80]
    return "Untitled application"


def load(path: Path = DEFAULT_PATH) -> List[Application]:
    """Load all records, newest first. Missing/corrupt file → empty list."""
    p = Path(path)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    out: List[Application] = []
    for item in data:
        try:
            out.append(Application.model_validate(item))
        except Exception:
            continue  # skip malformed rows, never break the list
    out.sort(key=lambda a: a.created_at, reverse=True)
    return out


def _write(path: Path, records: List[Application]) -> None:
    Path(path).write_text(
        json.dumps([r.model_dump() for r in records], indent=2),
        encoding="utf-8",
    )


def record(
    path: Path = DEFAULT_PATH,
    *,
    signals: Optional[JDSignals] = None,
    pointers: Optional[Pointers] = None,
    jd_text: str = "",
    ats_report: Optional[ATSReport] = None,
    from_target_role: bool = False,
    guard_dropped: int = 0,
    kind: str = "resume",
) -> Application:
    """Append one generation record and return it.

    Best-effort by contract: callers wrap this so a logging failure never
    breaks the actual download. Records are appended newest-last on disk but
    ``load`` returns newest-first.
    """
    app = Application(
        id=uuid.uuid4().hex[:12],
        created_at=_now_iso(),
        kind=kind,
        label=_derive_label(signals, jd_text),
        jd_snippet=_collapse_ws(jd_text)[:200],
        from_target_role=from_target_role,
        ats_score=(ats_report.score if ats_report is not None else None),
        ats_matched=(len(ats_report.matched) if ats_report is not None else 0),
        ats_total=(ats_report.total_checked if ats_report is not None else 0),
        length=(pointers.length if pointers else None),
        seniority=(pointers.seniority if pointers else None),
        context=(pointers.context if pointers else None),
        guard_dropped=guard_dropped,
    )
    # newest-last on disk (chronological); trim to the most recent MAX_RECORDS.
    existing = load(path)  # newest-first
    chronological = list(reversed(existing))
    chronological.append(app)
    if len(chronological) > MAX_RECORDS:
        chronological = chronological[-MAX_RECORDS:]
    _write(path, chronological)
    return app


def delete(app_id: str, path: Path = DEFAULT_PATH) -> bool:
    """Remove one record by id. Returns True if something was removed."""
    records = load(path)
    kept = [r for r in records if r.id != app_id]
    if len(kept) == len(records):
        return False
    _write(path, list(reversed(kept)))  # persist chronological
    return True


def update_status(
    app_id: str, status: str, path: Path = DEFAULT_PATH
) -> Optional[Application]:
    """Set the pipeline status of one record. Returns the updated record, or
    None if the id isn't found. Raises ValueError on an unknown status."""
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}; valid: {list(STATUSES)}")
    records = load(path)  # newest-first
    found: Optional[Application] = None
    for r in records:
        if r.id == app_id:
            r.status = status
            found = r
            break
    if found is None:
        return None
    _write(path, list(reversed(records)))  # persist chronological
    return found
