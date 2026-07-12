"""YAML and JSON load/save helpers."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from .schema import Master, Pointers, TailoredResume, Template


def _read_yaml(path: str | Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_master(path: str | Path) -> Master:
    data = _read_yaml(path)
    # Future migration hook: branch on data.get("schema_version", 1) and call
    # _migrate_from_v1(data) (etc.) before model_validate when a future
    # breaking change ships. Today every YAML loads as v1 (the default).
    return Master.model_validate(data)


def load_template(path: str | Path | None) -> Template:
    if path is None:
        return Template()
    return Template.model_validate(_read_yaml(path))


def load_pointers(path: str | Path | None) -> Pointers:
    if path is None:
        return Pointers()
    return Pointers.model_validate(_read_yaml(path) or {})


def load_jd_text(path: str | Path) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def save_tailored_json(tailored: TailoredResume, path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(tailored.model_dump(), f, indent=2)


def load_tailored_json(path: str | Path) -> TailoredResume:
    with open(path, "r", encoding="utf-8") as f:
        return TailoredResume.model_validate(json.load(f))


def pointers_from_dict(base: Pointers, overrides: dict) -> Pointers:
    """Merge override values into a base Pointers. Keys whose values are None or
    missing are skipped (the base value wins). For `must_include`, accepts either
    a list or a comma-separated string.

    Used by both the CLI (argparse Namespace -> dict) and the web UI (form -> dict)
    so the merge semantics live in one place.
    """
    data = base.model_dump()
    for key in ("length", "seniority", "context", "extra_instructions"):
        if overrides.get(key) is not None:
            data[key] = overrides[key]
    mi = overrides.get("must_include")
    if mi is not None:
        if isinstance(mi, str):
            mi = [s.strip() for s in mi.split(",") if s.strip()]
        data["must_include"] = list(mi)
    return Pointers.model_validate(data)


def _parse_year_month(value: str) -> tuple[int, int] | None:
    value = value.strip().lower()
    if not value or value in {"present", "current", "now"}:
        today = date.today()
        return today.year, today.month
    parts = value.split("-")
    try:
        year = int(parts[0])
        month = int(parts[1]) if len(parts) > 1 else 1
    except (TypeError, ValueError):
        return None
    if not 1 <= month <= 12:
        return None
    return year, month


def years_of_experience(master: Master) -> int:
    """Approximate years of experience from master.experience date ranges."""
    months = 0
    for exp in master.experience:
        start = _parse_year_month(exp.start)
        end = _parse_year_month(exp.end)
        if start is None or end is None:
            continue
        start_month = start[0] * 12 + start[1]
        end_month = end[0] * 12 + end[1]
        if end_month >= start_month:
            months += end_month - start_month + 1
    return months // 12


def apply_auto_length(master: Master, pointers: Pointers) -> Pointers:
    """Resolve missing/auto length pointer using Bock's one-page-per-decade rule."""
    if pointers.length not in {None, "auto"}:
        return pointers
    length = "2page" if years_of_experience(master) >= 10 else "1page"
    return pointers.model_copy(update={"length": length})
