"""Laszlo Bock XYZ bullet tier classifier.

Bock's target form is:
    Accomplished X as measured by Y by doing Z

This module is deliberately heuristic. It does not rewrite bullets; it gives
the user a fast, local signal about what a bullet is missing.
"""

from __future__ import annotations

import re
from typing import Literal

from .guard import _NUMBER_RE


BockTier = Literal["original", "better", "awesome"]
MissingPart = Literal["y_metric", "z_method", "x_strong_verb"]


_WEAK_OPENERS = (
    "responsible for",
    "helped",
    "helped with",
    "assisted",
    "worked on",
    "supported",
    "member of",
    "involved in",
    "participated in",
)

_Z_METHOD_RE = re.compile(
    # Require the word after "by/through/..." to start with a LETTER so
    # phrases like "Cut latency by 80%" (where "by" marks magnitude) don't
    # match as a method clause. The trailing `\w*` allows the rest of the
    # word to include digits.
    r"\b(?:by|through|using|via|leveraging)\s+[A-Za-z]\w*"
    r"|,\s*(?:\w+ing)\b",
    re.IGNORECASE,
)

# Phase 5 polish emits placeholder tokens like [METHOD] or [NUMBER] when
# the user hasn't supplied the missing piece yet. We strip these BEFORE
# checking for a metric / method so a bullet like "Cut latency by [NUMBER]%
# by [METHOD]" doesn't read as awesome — it's still missing the y + z.
_PLACEHOLDER_RE = re.compile(r"\[(?:NUMBER|METHOD|TIMEFRAME|SCOPE)\]")


def _strip_placeholders(text: str) -> str:
    return _PLACEHOLDER_RE.sub("", text)


def has_metric(text: str) -> bool:
    """Return True when the bullet includes a measurable Y.

    Placeholder tokens (``[NUMBER]``) don't count as real metrics.
    """
    return bool(_NUMBER_RE.search(_strip_placeholders(text)))


def has_method(text: str) -> bool:
    """Return True when the bullet names a plausible Z/how clause.

    Placeholder tokens (``[METHOD]``) don't count as real methods.
    """
    return bool(_Z_METHOD_RE.search(_strip_placeholders(text)))


def has_strong_opener(text: str) -> bool:
    """Return False for weak responsibility-style openers."""
    stripped = text.strip().lower()
    return not any(stripped.startswith(opener) for opener in _WEAK_OPENERS)


def classify_bullet(text: str) -> tuple[BockTier, list[MissingPart]]:
    """Classify a bullet as original / better / awesome.

    Returns ``(tier, missing)`` where missing can include:
    - ``y_metric``: no measurable number / dollar / percent / duration found
    - ``z_method``: no explicit "how" clause found
    - ``x_strong_verb``: starts with a weak responsibility phrase
    """
    missing: list[MissingPart] = []
    metric = has_metric(text)
    method = has_method(text)
    strong = has_strong_opener(text)

    if not metric:
        missing.append("y_metric")
    if not method:
        missing.append("z_method")
    if not strong:
        missing.append("x_strong_verb")

    if not metric or not strong:
        return "original", missing
    if not method:
        return "better", missing
    return "awesome", missing
