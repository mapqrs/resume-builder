"""ATS (Applicant Tracking System) lint — score the rendered resume against
the JD's key vocabulary.

Real ATS parsers strip the .docx to plain text and search for exact keyword
matches. A resume that misses too many JD keywords gets filtered before any
human sees it. This module:

1. Builds a keyword set from `jd_signals.top_keywords` + `pointers.must_include`
   + the inferred role_archetype + inferred_seniority (when set).
2. Counts how many appear (case-insensitive, word-boundary-aware) in the
   rendered text.
3. Returns a structured report with matched / missing / score / warnings.

The score is advisory — same posture as `lints.py`. The user can decide to
re-tailor with `--must-include "missing-kw1,missing-kw2"` to surface them.
"""

from __future__ import annotations

import re
from typing import List, Optional

from pydantic import BaseModel, Field

from .jd_signals import JDSignals
from .schema import Pointers


# Words that are too generic to be meaningful ATS signals. Even if they show
# up in top_keywords for some weird JD, we don't want to flag them as missing.
_ATS_NOISE = {
    "ic", "us", "uk", "eu", "co", "inc",
}


class ATSWarning(BaseModel):
    rule: str   # "score-low" | "score-critical" | "acronym-long-form"
    message: str


class ATSReport(BaseModel):
    score: float                              # 0.0 - 1.0
    matched: List[str] = Field(default_factory=list)
    missing: List[str] = Field(default_factory=list)
    total_checked: int = 0
    word_count: int = 0
    acronyms_missing_long_form: List[str] = Field(default_factory=list)
    warnings: List[ATSWarning] = Field(default_factory=list)


def _normalize_keyword(kw: str) -> str:
    return kw.strip().lower()


def _collect_keywords(
    signals: Optional[JDSignals], pointers: Optional[Pointers]
) -> List[str]:
    """Build the ordered, deduped keyword set the ATS check evaluates.

    Order matters for output stability: top_keywords first (by JD frequency),
    then must_include (explicit user signal), then role/seniority (single tokens).
    """
    seen: set[str] = set()
    out: List[str] = []

    def _add(kw: Optional[str]) -> None:
        if not kw:
            return
        norm = _normalize_keyword(kw)
        if not norm or norm in seen or norm in _ATS_NOISE:
            return
        seen.add(norm)
        out.append(kw.strip())

    if signals:
        for kw in signals.top_keywords:
            _add(kw)
    if pointers:
        for kw in pointers.must_include:
            _add(kw)
    if signals:
        _add(signals.role_archetype)
        _add(signals.inferred_seniority)
    return out


def _matches_in_text(keyword: str, text: str) -> bool:
    """True if `keyword` appears in `text` as a whole-word(s) match, case-insensitive.

    Works for single-word ("Postgres") and multi-word ("machine learning") keywords.
    For tokens that mix letters and digits (k8s, go), uses a relaxed boundary on
    the digit side so we don't fail on punctuation neighbors.
    """
    # \b doesn't behave on hyphens; treat hyphens as word characters by using
    # a custom boundary: (?:^|\W) ... (?:$|\W).
    pattern = r"(?:^|\W)" + re.escape(keyword) + r"(?:$|\W)"
    return re.search(pattern, text, re.IGNORECASE) is not None


_ACRONYM_RE = re.compile(r"\b[A-Z]{2,5}\b")
_ACRONYM_NOISE = {"USA", "US", "UK", "EU", "LLC", "INC", "HTTP", "HTTPS"}


def _has_long_form(acronym: str, text: str) -> bool:
    """Return True for patterns like Search Engine Optimization (SEO)."""
    pattern = (
        r"\b(?:[A-Z][A-Za-z]+|[a-z][a-z]+)"
        r"(?:[-\s]+(?:[A-Z][A-Za-z]+|[a-z][a-z]+)){1,5}"
        r"\s*\(\s*"
        + re.escape(acronym)
        + r"\s*\)"
    )
    return re.search(pattern, text) is not None


def _acronyms_missing_long_form(text: str) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for m in _ACRONYM_RE.finditer(text):
        acronym = m.group(0)
        if acronym in seen or acronym in _ACRONYM_NOISE:
            continue
        seen.add(acronym)
        if not _has_long_form(acronym, text):
            out.append(acronym)
    return out


def score_ats(
    docx_text: str,
    signals: Optional[JDSignals] = None,
    pointers: Optional[Pointers] = None,
) -> ATSReport:
    """Score the rendered text against the JD's keyword set.

    `docx_text` is the plain-text extraction of the rendered .docx — what an
    ATS parser would see. Returns an ATSReport with score + matched + missing.
    """
    keywords = _collect_keywords(signals, pointers)
    word_count = len(re.findall(r"\b\w+\b", docx_text))
    missing_long_forms = _acronyms_missing_long_form(docx_text)
    acronym_warnings: List[ATSWarning] = []
    if len(missing_long_forms) >= 3:
        acronym_warnings.append(
            ATSWarning(
                rule="acronym-long-form",
                message=(
                    "ATS acronym check: spell out acronyms once, e.g. "
                    f"`Search Engine Optimization (SEO)`. Missing long forms for: "
                    + ", ".join(missing_long_forms[:6])
                    + ("…" if len(missing_long_forms) > 6 else "")
                ),
            )
        )

    if not keywords:
        return ATSReport(
            score=1.0,
            matched=[],
            missing=[],
            total_checked=0,
            word_count=word_count,
            acronyms_missing_long_form=missing_long_forms,
            warnings=acronym_warnings,
        )

    matched: List[str] = []
    missing: List[str] = []
    for kw in keywords:
        if _matches_in_text(kw, docx_text):
            matched.append(kw)
        else:
            missing.append(kw)

    score = len(matched) / len(keywords)
    warnings: List[ATSWarning] = []
    if score < 0.60:
        warnings.append(
            ATSWarning(
                rule="score-critical",
                message=(
                    f"ATS score {int(score * 100)}% — many JD keywords missing. "
                    "Re-tailor with `--must-include` covering: "
                    + ", ".join(missing[:6])
                    + ("…" if len(missing) > 6 else "")
                ),
            )
        )
    elif score < 0.80:
        warnings.append(
            ATSWarning(
                rule="score-low",
                message=(
                    f"ATS score {int(score * 100)}% — consider surfacing: "
                    + ", ".join(missing[:5])
                    + ("…" if len(missing) > 5 else "")
                ),
            )
        )
    warnings.extend(acronym_warnings)

    return ATSReport(
        score=score,
        matched=matched,
        missing=missing,
        total_checked=len(keywords),
        word_count=word_count,
        acronyms_missing_long_form=missing_long_forms,
        warnings=warnings,
    )
