"""Named template presets for the final resume render.

Three curated formats the user can pick from, plus guidance about which
one to use based on years of experience and the page-count target.

The presets just hand back ``Template`` objects with sensible defaults —
the existing render pipeline consumes them unchanged. The web UI exposes
the picker; the CLI exposes ``--preset <name>``.

Presets
-------

- ``bock-classic-1pg`` — Default. ATS-friendly single-page, Bock-aligned
  conventions. Tight margins, conservative typography, classic section
  order. The "safe default" — works in any industry, any seniority.
- ``detailed-2pg`` — Two-page, room for context. Larger margins, slightly
  bigger fonts, projects + skills get more breathing room. For 8+ years
  of experience where compressing into one page would drop signal.
- ``modern-compact-1pg`` — Single page, modern type, slightly tighter
  spacing. Subtle accent color on section headings. For senior engineers
  / product folks who want polish without crossing into "designer
  resume" territory.

Each preset also carries a ``length_pointer`` hint the tailor uses to
bias bullet selection — ``"1page"`` vs ``"2page"``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from .schema import (
    ColorsSpec,
    FontSpec,
    FontsSpec,
    PageSpec,
    SpacingSpec,
    Template,
)


@dataclass(frozen=True)
class PresetGuidance:
    """User-facing copy explaining when to pick a preset."""

    best_for: str          # one-line "best for X" tag
    pages: str             # "1 page" / "2 pages" / "1-2 pages"
    years_experience: str  # "0-10 years" etc.
    notes: str             # 1-2 sentences of nuance


@dataclass(frozen=True)
class PresetSpec:
    """A named formatting preset the user can pick from the UI / CLI."""

    id: str
    label: str
    template: Template
    length_pointer: str  # "1page" | "2page"
    guidance: PresetGuidance


def _build_bock_classic() -> Template:
    return Template(
        page=PageSpec(
            size="letter",
            margin_top="0.5in",
            margin_bottom="0.5in",
            margin_left="0.7in",
            margin_right="0.7in",
        ),
        fonts=FontsSpec(
            body=FontSpec(name="Calibri", size=10.5),
            heading=FontSpec(name="Calibri", size=12, bold=True),
            name=FontSpec(name="Calibri", size=18, bold=True),
            role=FontSpec(name="Calibri", size=11, bold=True),
        ),
        spacing=SpacingSpec(line=1.15, paragraph_after=4.0),
        colors=ColorsSpec(heading="#000000", accent="#2E5C8A", body="#000000"),
        section_order=["summary", "experience", "projects", "education", "skills"],
    )


def _build_detailed_2pg() -> Template:
    return Template(
        page=PageSpec(
            size="letter",
            margin_top="0.7in",
            margin_bottom="0.7in",
            margin_left="0.8in",
            margin_right="0.8in",
        ),
        fonts=FontsSpec(
            body=FontSpec(name="Calibri", size=11.0),
            heading=FontSpec(name="Calibri", size=13, bold=True),
            name=FontSpec(name="Calibri", size=20, bold=True),
            role=FontSpec(name="Calibri", size=12, bold=True),
        ),
        spacing=SpacingSpec(line=1.2, paragraph_after=6.0),
        colors=ColorsSpec(heading="#000000", accent="#2E5C8A", body="#000000"),
        section_order=["summary", "experience", "projects", "education", "skills"],
    )


def _build_modern_compact() -> Template:
    return Template(
        page=PageSpec(
            size="letter",
            margin_top="0.45in",
            margin_bottom="0.45in",
            margin_left="0.65in",
            margin_right="0.65in",
        ),
        fonts=FontsSpec(
            body=FontSpec(name="Calibri", size=10.0),
            heading=FontSpec(name="Calibri", size=11.5, bold=True),
            name=FontSpec(name="Calibri", size=17, bold=True),
            role=FontSpec(name="Calibri", size=10.5, bold=True),
        ),
        spacing=SpacingSpec(line=1.1, paragraph_after=3.0),
        # Tasteful accent on the section headings — but body stays black so
        # the render still prints fine in greyscale + passes ATS.
        colors=ColorsSpec(heading="#1F3D5C", accent="#1F3D5C", body="#000000"),
        section_order=["summary", "experience", "projects", "skills", "education"],
    )


PRESETS: List[PresetSpec] = [
    PresetSpec(
        id="bock-classic-1pg",
        label="Bock Classic · 1 page",
        template=_build_bock_classic(),
        length_pointer="1page",
        guidance=PresetGuidance(
            best_for="ATS-safe default. Pick this when in doubt.",
            pages="1 page",
            years_experience="0-10 years",
            notes=(
                "Single page, conservative typography, classic section order. "
                "Works in any industry. Bock's recommended baseline."
            ),
        ),
    ),
    PresetSpec(
        id="detailed-2pg",
        label="Detailed · 2 pages",
        template=_build_detailed_2pg(),
        length_pointer="2page",
        guidance=PresetGuidance(
            best_for="Senior / staff roles with 8+ years to show.",
            pages="2 pages",
            years_experience="8+ years",
            notes=(
                "More breathing room — slightly bigger fonts, generous "
                "margins, projects + skills get full sections. Compressing "
                "your career into one page would drop signal."
            ),
        ),
    ),
    PresetSpec(
        id="modern-compact-1pg",
        label="Modern Compact · 1 page",
        template=_build_modern_compact(),
        length_pointer="1page",
        guidance=PresetGuidance(
            best_for="Senior engineers / PMs who want polish.",
            pages="1 page",
            years_experience="5+ years",
            notes=(
                "Tighter spacing, subtle accent color on section headings, "
                "skills surfaced above education. Still ATS-safe (body stays "
                "plain black). Not a designer-resume."
            ),
        ),
    ),
]


def get_preset(preset_id: str) -> PresetSpec:
    """Look up a preset by id. Raises KeyError if unknown."""
    for p in PRESETS:
        if p.id == preset_id:
            return p
    raise KeyError(f"unknown preset: {preset_id!r}")


def default_preset_id() -> str:
    """The id used when the user makes no choice."""
    return "bock-classic-1pg"


def preset_for_years_experience(years: int) -> PresetSpec:
    """Suggest a preset based on the user's years of experience.

    The wizard can call this to pre-select a reasonable default when
    the user lands on the preset picker without explicit input.
    """
    if years >= 8:
        return get_preset("detailed-2pg")
    if years >= 5:
        return get_preset("modern-compact-1pg")
    return get_preset("bock-classic-1pg")


def all_presets_for_ui() -> list[dict]:
    """Serialise every preset for the picker UI (JSON-ready)."""
    return [
        {
            "id": p.id,
            "label": p.label,
            "length_pointer": p.length_pointer,
            "guidance": {
                "best_for": p.guidance.best_for,
                "pages": p.guidance.pages,
                "years_experience": p.guidance.years_experience,
                "notes": p.guidance.notes,
            },
            "is_default": p.id == default_preset_id(),
        }
        for p in PRESETS
    ]
