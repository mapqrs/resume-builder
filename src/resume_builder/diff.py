"""Build a side-by-side master ↔ tailored diff payload for the UI.

For each master bullet, classify what happened:
- "kept"          — the tailor surfaced it (with possible rewrite)
- "dropped"       — the tailor chose not to surface it
- "guard-dropped" — the tailor surfaced it, but the no-invention guard
                    rejected the rewrite (with a reason)

The summary is included as a top-level field so the UI can show
master summary vs. tailored summary on its own row.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

from .guard import GuardWarning
from .schema import Master, TailoredResume


class DiffBullet(BaseModel):
    source_id: str
    master_text: str
    master_tags: List[str] = Field(default_factory=list)
    master_impact: Optional[int] = None
    rewritten: Optional[str] = None       # what the LLM produced (kept OR rejected)
    kind: str                              # "kept" | "dropped" | "guard-dropped"
    guard_reason: Optional[str] = None     # populated when kind == "guard-dropped"


class DiffItem(BaseModel):
    container_id: str
    container_label: str
    container_kind: str                    # "experience" | "project"
    bullets: List[DiffBullet]


class DiffStats(BaseModel):
    kept: int = 0
    dropped: int = 0
    guard_dropped: int = 0
    total: int = 0


class DiffPayload(BaseModel):
    summary_master: Optional[str] = None
    summary_tailored: Optional[str] = None
    summary_changed: bool = False
    sections: List[DiffItem]
    stats: DiffStats
    rationale: Optional[str] = None


def _index_tailored(tailored: TailoredResume) -> dict[str, dict]:
    """Map bullet_source_id -> {'container_id', 'rewritten'} for fast lookup."""
    out: dict[str, dict] = {}
    for section in tailored.sections:
        for item in section.items:
            for b in item.bullets:
                out[b.source_id] = {
                    "container_id": item.source_id,
                    "rewritten": b.rewritten_text,
                }
    return out


def _index_guard(warnings: list[GuardWarning]) -> dict[str, tuple[str, str]]:
    """Map bullet_source_id -> (attempted_text, reason) for guard-dropped bullets."""
    return {
        w.bullet_source_id: (w.rewritten_text, w.reason)
        for w in warnings
        # Skip the synthetic "<summary>" sentinel (handled separately).
        if w.bullet_source_id != "<summary>"
    }


def build_diff(
    master: Master,
    tailored: TailoredResume,
    guard_warnings: Optional[List[GuardWarning]] = None,
) -> DiffPayload:
    """Build a flat, render-ready payload describing how the master was tailored.

    `tailored` should be the CLEANED tailored output (post-guard). guard_warnings
    is the list of GuardWarning dropped during validation; pass it to surface
    guard-rejected bullets in the diff alongside the master entry.
    """
    guard_warnings = guard_warnings or []
    surfaced = _index_tailored(tailored)
    guard_dropped = _index_guard(guard_warnings)

    stats = DiffStats()
    sections: List[DiffItem] = []

    def _classify(bullet) -> DiffBullet:
        stats.total += 1
        if bullet.id in surfaced:
            stats.kept += 1
            return DiffBullet(
                source_id=bullet.id,
                master_text=bullet.text,
                master_tags=bullet.tags,
                master_impact=bullet.impact_score,
                rewritten=surfaced[bullet.id]["rewritten"],
                kind="kept",
            )
        if bullet.id in guard_dropped:
            attempted, reason = guard_dropped[bullet.id]
            stats.guard_dropped += 1
            return DiffBullet(
                source_id=bullet.id,
                master_text=bullet.text,
                master_tags=bullet.tags,
                master_impact=bullet.impact_score,
                rewritten=attempted,
                kind="guard-dropped",
                guard_reason=reason,
            )
        stats.dropped += 1
        return DiffBullet(
            source_id=bullet.id,
            master_text=bullet.text,
            master_tags=bullet.tags,
            master_impact=bullet.impact_score,
            rewritten=None,
            kind="dropped",
        )

    for exp in master.experience:
        sections.append(
            DiffItem(
                container_id=exp.id,
                container_label=f"{exp.role} @ {exp.company}",
                container_kind="experience",
                bullets=[_classify(b) for b in exp.bullets],
            )
        )
    for proj in master.projects:
        sections.append(
            DiffItem(
                container_id=proj.id,
                container_label=proj.name,
                container_kind="project",
                bullets=[_classify(b) for b in proj.bullets],
            )
        )

    summary_master = master.summary
    summary_tailored = tailored.summary
    summary_changed = bool(
        (summary_master or "").strip() != (summary_tailored or "").strip()
        and (summary_master or summary_tailored)
    )

    return DiffPayload(
        summary_master=summary_master,
        summary_tailored=summary_tailored,
        summary_changed=summary_changed,
        sections=sections,
        stats=stats,
        rationale=tailored.rationale,
    )
