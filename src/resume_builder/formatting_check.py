"""Bock formatting compliance checks for resume templates."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from .schema import Template


Severity = Literal["warning", "error"]


@dataclass
class FormattingWarning:
    rule: str
    message: str
    severity: Severity = "warning"


_LENGTH_RE = re.compile(r"^\s*([\d.]+)\s*(in|cm|mm|pt)?\s*$", re.IGNORECASE)


def _length_to_inches(value: str) -> float:
    m = _LENGTH_RE.match(value)
    if not m:
        raise ValueError(f"Invalid length: {value!r}")
    n = float(m.group(1))
    unit = (m.group(2) or "in").lower()
    if unit == "in":
        return n
    if unit == "cm":
        return n / 2.54
    if unit == "mm":
        return n / 25.4
    if unit == "pt":
        return n / 72
    raise ValueError(f"Unknown unit: {unit}")


def check_template(template: Template) -> list[FormattingWarning]:
    """Return Bock formatting warnings for a template."""
    warnings: list[FormattingWarning] = []
    if template.fonts.body.size < 11.0:
        warnings.append(
            FormattingWarning(
                rule="bock-font-size",
                message=(
                    f"body font is {template.fonts.body.size:g}pt; "
                    "Bock recommends 11pt minimum."
                ),
            )
        )

    for attr, label in (
        ("margin_top", "top"),
        ("margin_bottom", "bottom"),
        ("margin_left", "left"),
        ("margin_right", "right"),
    ):
        raw = getattr(template.page, attr)
        try:
            inches = _length_to_inches(raw)
        except ValueError as e:
            warnings.append(
                FormattingWarning(
                    rule="bock-margin-parse",
                    message=str(e),
                    severity="error",
                )
            )
            continue
        if inches > 0.5:
            warnings.append(
                FormattingWarning(
                    rule="bock-margin",
                    message=(
                        f"{label} margin is {raw}; Bock recommends 0.5in or tighter."
                    ),
                )
            )
    return warnings
