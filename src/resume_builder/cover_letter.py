"""Cover letter generation — same data model and modes as the resume tailor.

Three paths to a CoverLetter:
- `cover_letter_via_claude_cli()` — shell out to `claude -p` (auto mode)
- `write_cover_letter_prompt_for_paste()` — emit prompt text for any Claude UI
- `parse_cover_letter_response_text()` — parse the JSON reply back

The cover letter guard is the prose analogue of `guard.validate`. Each
paragraph carries `source_ids`; the guard checks that numbers and proper
nouns in `paragraph.text` appear in the union of those bullets'
`text + variants`, OR the JD vocabulary, OR the must-include allowlist.
Generic statements of interest (no concrete claim) pass naturally.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .guard import (
    _build_jd_vocab,
    _build_pointer_vocab,
    _check_bullet,
)
from .jd_signals import JDSignals, extract as extract_signals
from .llm import (
    ClaudeCodeProvider,
    CopyPasteRequired,
    LLMError,
    LLMProvider,
    pick_provider,
)
from .prompts import (
    COVER_LETTER_SYSTEM_PROMPT,
    build_cover_letter_user_message,
)
from .schema import CoverLetter, CoverLetterParagraph, Master, Pointers
from .tailor import (
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT_S,
    ClaudeCliError,
    _extract_first_json_object,
)


# ---------- parsing ----------


def parse_cover_letter_response_text(text: str) -> CoverLetter:
    """Parse a Claude response (JSON, possibly with surrounding prose / fences)."""
    payload = _extract_first_json_object(text)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Cover letter: response was not valid JSON ({e}). First 500 chars:\n{text[:500]}"
        ) from e
    return CoverLetter.model_validate(data)


def parse_cover_letter_response_file(path: str | Path) -> CoverLetter:
    return parse_cover_letter_response_text(Path(path).read_text(encoding="utf-8"))


# ---------- generation: auto (claude -p) ----------


def cover_letter_via_provider(
    master: Master,
    jd_text: str,
    pointers: Pointers,
    provider: LLMProvider,
    *,
    model: Optional[str] = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    signals: Optional[JDSignals] = None,
) -> CoverLetter:
    """Generate a CoverLetter via any ``LLMProvider``. ``CopyPasteRequired``
    propagates so callers can render the paste UI.
    """
    if signals is None:
        signals = extract_signals(jd_text)
    user_msg = build_cover_letter_user_message(
        master, jd_text, pointers, signals=signals,
    )
    try:
        result_text = provider.complete(
            COVER_LETTER_SYSTEM_PROMPT, user_msg,
            model=model, timeout_s=timeout_s,
        )
    except CopyPasteRequired:
        raise
    except LLMError as e:
        raise ClaudeCliError(str(e)) from e
    return parse_cover_letter_response_text(result_text)


def cover_letter_via_claude_cli(
    master: Master,
    jd_text: str,
    pointers: Pointers,
    *,
    model: str = DEFAULT_MODEL,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    signals: Optional[JDSignals] = None,
) -> CoverLetter:
    """Back-compat shim that pins the provider to ``ClaudeCodeProvider``."""
    return cover_letter_via_provider(
        master, jd_text, pointers, ClaudeCodeProvider(),
        model=model, timeout_s=timeout_s, signals=signals,
    )


def cover_letter_auto(
    master: Master,
    jd_text: str,
    pointers: Pointers,
    *,
    model: Optional[str] = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    signals: Optional[JDSignals] = None,
) -> tuple[CoverLetter, str]:
    """Generate via ``pick_provider()``. Returns ``(letter, reason)``."""
    choice = pick_provider()
    letter = cover_letter_via_provider(
        master, jd_text, pointers, choice.provider,
        model=model, timeout_s=timeout_s, signals=signals,
    )
    return letter, choice.reason


# ---------- generation: copy-paste mode ----------


def write_cover_letter_prompt_for_paste(
    master: Master,
    jd_text: str,
    pointers: Pointers,
    out_path: str | Path,
    signals: Optional[JDSignals] = None,
) -> Path:
    if signals is None:
        signals = extract_signals(jd_text)
    full = (
        "# === SYSTEM (paste as the start of your message, or into a system-prompt slot) ===\n"
        + COVER_LETTER_SYSTEM_PROMPT
        + "\n\n"
        + "# === USER MESSAGE ===\n"
        + build_cover_letter_user_message(master, jd_text, pointers, signals=signals)
        + "\n\n"
        + "# === INSTRUCTIONS ===\n"
        "# Paste everything above into a Claude conversation.\n"
        "# Save Claude's JSON reply to a file (just the JSON, no markdown fences).\n"
        "# Then re-run with --cover-from-response <that-file>.\n"
    )
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(full, encoding="utf-8")
    return path


# ---------- guard (no-invention, prose flavor) ----------


@dataclass
class CoverLetterWarning:
    paragraph_index: int
    paragraph_role: str
    reason: str
    text: str


@dataclass
class CoverLetterGuardResult:
    cleaned: CoverLetter
    warnings: List[CoverLetterWarning] = field(default_factory=list)


# Words that the guard's proper-noun extractor surfaces from prose but that
# don't represent fabrication if the LLM uses them in a salutation/intro.
# These are generic cover-letter vocabulary, not invented credentials.
_PROSE_ALLOWLIST = {
    # letter-writing
    "dear", "sincerely", "regards", "best", "warmly", "respectfully",
    # generic role/position vocab
    "team", "hiring", "manager", "managers", "company", "role", "roles",
    "position", "positions", "opportunity", "opportunities", "thank",
    "thanks", "candidate", "candidates", "application", "applicant",
    "applicants", "interview",
    # seniority levels (universal English; not invented credentials)
    "senior", "staff", "principal", "junior", "lead", "founding",
    "associate", "director", "vp",
    # role descriptors that are universal English (the candidate IS an engineer)
    "engineer", "engineers", "engineering", "software", "developer",
    "developers", "ic",
    # role-archetype labels (also captured separately by jd_signals)
    "backend", "frontend", "fullstack", "infrastructure", "infra",
    "platform", "mobile", "security",
}

_GENERIC_SALUTATION_RE = re.compile(
    r"^\s*(?:dear\s+hiring\s+(?:manager|team)|to\s+whom\s+it\s+may\s+concern)\b",
    re.IGNORECASE,
)


# Bock Part 03: a cover letter should be tight — every paragraph earns its
# place. These are soft caps (warning, not rejection) tuned to Bock's prose
# advice + the COVER_LETTER_SYSTEM_PROMPT's per-paragraph guidance.
_PARAGRAPH_WORD_CAPS: dict[str, int] = {
    "intro": 35,
    "expand": 90,
    "why_role": 90,
    "why_me": 90,
    "close": 35,
}
_TOTAL_WORD_CAP = 400  # Bock: cover letters > 1 page get skipped.


def _word_count(text: str) -> int:
    """Whitespace-split word count. Good enough for cover-letter prose."""
    return len(text.split())


def _master_metadata_vocab(master: Master) -> set[str]:
    """Proper-noun-like tokens that come from the master's *metadata* — company
    names, role titles, project names, school names. The bullet-level guard
    doesn't see these (it only reads bullet text), but cover-letter prose
    legitimately references them. Extracted once per call.
    """
    from .guard import _PROPER_NOUN_RE, _COMMON_STARTERS

    blob_parts: List[str] = []
    for exp in master.experience:
        blob_parts.append(exp.company)
        blob_parts.append(exp.role)
        if exp.location:
            blob_parts.append(exp.location)
    for proj in master.projects:
        blob_parts.append(proj.name)
    for edu in master.education:
        blob_parts.append(edu.school)
        if edu.location:
            blob_parts.append(edu.location)
    for grp in master.skills:
        blob_parts.extend(grp.items)
    blob = " ".join(blob_parts)

    out: set[str] = set()
    for m in _PROPER_NOUN_RE.finditer(blob):
        token = m.group(1)
        if len(token) < 2 or token.lower() in _COMMON_STARTERS:
            continue
        out.add(token.lower())
    return out


def validate_cover_letter(
    master: Master,
    cl: CoverLetter,
    jd_text: str,
    pointers: Optional[Pointers] = None,
) -> CoverLetterGuardResult:
    """Validate cover letter paragraphs against fabrication.

    For each paragraph: build the legal vocabulary as the union of
    (referenced source bullets' text + variants) + master.summary + the JD.
    Then run the same number/proper-noun check used by the resume guard.

    Paragraphs that fail are NOT dropped — they're kept in `cleaned`, but the
    warning surfaces so the user can re-prompt or accept the risk.
    """
    pointers = pointers or Pointers()
    # The "JD vocab" passed to _check_bullet is permissively augmented for
    # cover letters: JD tokens + universal letter vocab + master metadata
    # (company names, role titles, project names, school names, skills).
    jd_vocab = (
        _build_jd_vocab(jd_text)
        | _PROSE_ALLOWLIST
        | _master_metadata_vocab(master)
    )
    pointer_vocab = _build_pointer_vocab(pointers)
    signals = extract_signals(jd_text)

    warnings: List[CoverLetterWarning] = []
    valid_bullet_ids = master.all_bullet_ids()

    if _GENERIC_SALUTATION_RE.search(cl.salutation):
        warnings.append(
            CoverLetterWarning(
                paragraph_index=-1,
                paragraph_role="salutation",
                reason=(
                    "generic salutation; Bock recommends finding a real name "
                    "or at least a more specific team."
                ),
                text=cl.salutation,
            )
        )

    roles = [p.role for p in cl.paragraphs]
    normalized_roles = ["expand" if r == "why_me" else r for r in roles]
    expected_roles = ["intro", "expand", "why_role", "close"]
    if normalized_roles != expected_roles:
        warnings.append(
            CoverLetterWarning(
                paragraph_index=-1,
                paragraph_role="structure",
                reason=(
                    "cover letter should use exactly 4 paragraphs in order: "
                    "intro, expand, why_role, close."
                ),
                text=" / ".join(roles),
            )
        )

    # Total word-count check — Bock cuts cover letters longer than one page.
    total_words = sum(_word_count(p.text) for p in cl.paragraphs)
    if total_words > _TOTAL_WORD_CAP:
        warnings.append(
            CoverLetterWarning(
                paragraph_index=-1,
                paragraph_role="length",
                reason=(
                    f"cover letter is {total_words} words; Bock's rule of thumb "
                    f"is keep it under {_TOTAL_WORD_CAP} words (≤1 page)."
                ),
                text="",
            )
        )

    for i, para in enumerate(cl.paragraphs):
        # Per-paragraph length check — soft cap.
        cap = _PARAGRAPH_WORD_CAPS.get(para.role)
        if cap is not None:
            wc = _word_count(para.text)
            if wc > cap:
                warnings.append(
                    CoverLetterWarning(
                        paragraph_index=i,
                        paragraph_role=para.role,
                        reason=(
                            f"{para.role} paragraph is {wc} words; Bock prefers "
                            f"≤{cap} for this role."
                        ),
                        text=para.text,
                    )
                )

        # Collect the union of legal source text for this paragraph.
        legal_parts: List[str] = []
        for sid in para.source_ids:
            if sid not in valid_bullet_ids:
                warnings.append(
                    CoverLetterWarning(
                        paragraph_index=i,
                        paragraph_role=para.role,
                        reason=f"source_id {sid!r} not in master",
                        text=para.text,
                    )
                )
                continue
            bullet = master.bullet_by_id(sid)
            if bullet is not None:
                legal_parts.extend(bullet.all_source_texts())

        # The master summary and the basics text are also legal for prose
        # (cover letters often reference the candidate's overall positioning).
        if master.summary:
            legal_parts.append(master.summary)
        legal_combined = "\n".join(legal_parts)

        # Numbers/proper-noun extraction reuses the existing guard helpers.
        reason = _check_bullet(para.text, legal_combined, jd_vocab, pointer_vocab)
        if reason:
            warnings.append(
                CoverLetterWarning(
                    paragraph_index=i,
                    paragraph_role=para.role,
                    reason=reason,
                    text=para.text,
                )
            )

        if para.role == "why_role":
            specifics = signals.company_specifics
            jd_proper_nouns = _build_jd_vocab(jd_text) - _PROSE_ALLOWLIST
            para_lower = para.text.lower()
            has_specific = any(
                token.lower() in para_lower
                for phrase in specifics
                for token in re.findall(r"[A-Za-z][A-Za-z0-9\-]{2,}", phrase)
            ) or any(token in para_lower for token in jd_proper_nouns)
            if not has_specific:
                warnings.append(
                    CoverLetterWarning(
                        paragraph_index=i,
                        paragraph_role=para.role,
                        reason=(
                            "why_role paragraph lacks a company-specific fact "
                            "from the JD or company signals."
                        ),
                        text=para.text,
                    )
                )

    return CoverLetterGuardResult(cleaned=cl, warnings=warnings)


# ---------- plain-text rendering (for UI / file) ----------


def cover_letter_to_plain_text(cl: CoverLetter, master: Master) -> str:
    """Render the cover letter as plain text. Header pulled from master.basics."""
    lines: List[str] = []
    b = master.basics
    lines.append(b.name)
    contact_bits = [s for s in (b.email, b.phone, b.location) if s]
    if contact_bits:
        lines.append(" · ".join(contact_bits))
    if b.links:
        lines.append(" · ".join(f"{lk.label}: {lk.url}" for lk in b.links))
    lines.append("")
    lines.append(cl.salutation)
    lines.append("")
    for p in cl.paragraphs:
        lines.append(p.text.strip())
        lines.append("")
    lines.append(cl.closing)
    lines.append(b.name)
    return "\n".join(lines).rstrip() + "\n"
