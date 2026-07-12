"""No-invention guard: validates that tailored bullets only contain claims grounded
in the master + JD vocabulary. Drops bullets that fail.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from .schema import (
    Master,
    Pointers,
    TailoredBullet,
    TailoredItem,
    TailoredResume,
    TailoredSection,
)


# Numeric tokens, with optional magnitude (K/M/B) or unit (ms/s/x) suffix.
# Captured as one token so "$99K" doesn't slip past as just "99" + ignored "K".
# Matches: 5, 5.2, 5%, $40K, 12M, 2B, 480ms, 30s, 2x, 200, 1.2k
_NUMBER_RE = re.compile(
    r"(?<![\w])(\d+(?:[\.,]\d+)?(?:K|M|B|ms|s|x)?)\b",
    re.IGNORECASE,
)

# Capitalized tech-y proper nouns. Matches:
#   - Words starting with uppercase letter, length >= 2 (Acme, Postgres)
#   - All-caps acronyms (AWS, SQL, CI)
#   - Mixed case like Kubernetes, ScyllaDB, TypeScript, GraphQL, k8s
# Excludes sentence-start "The/A/I" via downstream allowlist.
_PROPER_NOUN_RE = re.compile(r"\b([A-Z][A-Za-z0-9]*(?:[A-Z][A-Za-z0-9]*)*|[a-z]+[A-Z][A-Za-z0-9]*|k8s)\b")

# Common English words that happen to be capitalized at sentence start. Don't flag these.
_COMMON_STARTERS = {
    "the", "a", "an", "and", "or", "but", "if", "i", "we", "you", "they",
    "this", "that", "these", "those", "for", "of", "in", "on", "at", "to",
    "from", "with", "by", "as", "is", "was", "are", "were", "be", "been",
    "being", "have", "has", "had", "do", "does", "did", "will", "would",
    "should", "could", "may", "might", "must", "can", "shall", "led",
    "built", "designed", "shipped", "owned", "wrote", "reduced", "drove",
    "scaled", "delivered", "developed", "implemented", "launched",
    "improved", "introduced", "managed", "mentored", "partnered", "ran",
    "created", "established", "co-led", "spearheaded", "optimized",
}


@dataclass
class GuardWarning:
    bullet_source_id: str
    rewritten_text: str
    reason: str


@dataclass
class GuardResult:
    cleaned: TailoredResume
    warnings: List[GuardWarning] = field(default_factory=list)
    dropped_bullet_ids: List[str] = field(default_factory=list)


def _normalize_number(s: str) -> str:
    # Strip thousands separators and lowercase the magnitude suffix.
    # "5K" → "5k", "1,200" → "1200", "480MS" → "480ms".
    return s.replace(",", "").lower()


def _extract_numbers(text: str) -> set[str]:
    return {_normalize_number(m.group(1)) for m in _NUMBER_RE.finditer(text)}


def _is_sentence_start(text: str, idx: int) -> bool:
    """True if position `idx` is the first non-whitespace character of a sentence."""
    if idx == 0:
        return True
    # Walk back over whitespace
    i = idx - 1
    while i >= 0 and text[i].isspace():
        i -= 1
    if i < 0:
        return True
    return text[i] in ".!?"


def _extract_proper_nouns(text: str) -> set[str]:
    """Return proper-noun-like tokens from `text`, lowercased for matching."""
    out: set[str] = set()
    for m in _PROPER_NOUN_RE.finditer(text):
        token = m.group(1)
        # Skip pure-digit tokens — handled by number check
        if token.isdigit():
            continue
        # Skip very short tokens (1 char)
        if len(token) < 2:
            continue
        # Skip common English starters even mid-sentence
        if token.lower() in _COMMON_STARTERS:
            continue
        # Skip the first word of any sentence — it's just capitalization, not a proper noun.
        # An all-caps acronym (AWS, SQL) or a clearly tech-y CamelCase token (ScyllaDB) is
        # still flagged because it's not "just capitalized" — it has interior caps.
        if _is_sentence_start(text, m.start()) and not _looks_like_real_proper_noun(token):
            continue
        out.add(token.lower())
    return out


def _looks_like_real_proper_noun(token: str) -> bool:
    """A token like 'AWS', 'GraphQL', 'ScyllaDB' is a real proper noun even at sentence start.
    A token like 'Migrated', 'Designed', 'Built' is just a capitalized verb.
    Heuristic: has interior capitals, OR is all-caps and >= 2 chars, OR mixes letters and digits.
    """
    if token.isupper() and len(token) >= 2 and token.isalpha():
        return True  # AWS, SQL, CI, etc.
    # Interior capital: not all-lowercase after first char
    if any(c.isupper() for c in token[1:]):
        return True  # ScyllaDB, GraphQL, TypeScript
    # Letter+digit mix (k8s, S3)
    if any(c.isdigit() for c in token) and any(c.isalpha() for c in token):
        return True
    return False


def _build_jd_vocab(jd_text: str) -> set[str]:
    """Set of lowercased tokens present in the JD (proper-noun candidates)."""
    vocab: set[str] = set()
    for m in _PROPER_NOUN_RE.finditer(jd_text):
        token = m.group(1)
        if token.lower() in _COMMON_STARTERS:
            continue
        if len(token) < 2:
            continue
        vocab.add(token.lower())
    return vocab


def _build_pointer_vocab(pointers: Pointers) -> set[str]:
    """Must-include keywords are explicitly authorized to appear."""
    return {kw.lower() for kw in pointers.must_include}


def _check_bullet(
    rewritten_text: str,
    source_text: str,
    jd_vocab: set[str],
    pointer_vocab: set[str],
) -> Optional[str]:
    """Return failure reason or None if the bullet passes."""
    # Numbers must literally appear in the source bullet text
    out_numbers = _extract_numbers(rewritten_text)
    src_numbers = _extract_numbers(source_text)
    rogue_numbers = out_numbers - src_numbers
    if rogue_numbers:
        return f"introduced number(s) not in source: {sorted(rogue_numbers)}"

    # Proper nouns must be in source text OR JD vocab OR pointer vocab
    out_nouns = _extract_proper_nouns(rewritten_text)
    src_nouns = _extract_proper_nouns(source_text)
    rogue_nouns = out_nouns - src_nouns - jd_vocab - pointer_vocab
    if rogue_nouns:
        return (
            f"introduced proper noun(s) not in source/JD/pointers: {sorted(rogue_nouns)}"
        )

    return None


def validate(
    master: Master,
    tailored: TailoredResume,
    jd_text: str,
    pointers: Optional[Pointers] = None,
) -> GuardResult:
    """Strip any tailored bullet that fabricates content; return cleaned + warnings."""
    pointers = pointers or Pointers()
    jd_vocab = _build_jd_vocab(jd_text)
    pointer_vocab = _build_pointer_vocab(pointers)

    valid_bullet_ids = master.all_bullet_ids()

    cleaned_sections: List[TailoredSection] = []
    warnings: List[GuardWarning] = []
    dropped: List[str] = []

    for section in tailored.sections:
        cleaned_items: List[TailoredItem] = []
        for item in section.items:
            container = master.container_by_id(item.source_id)
            if container is None:
                # Whole container not in master — drop entirely
                for b in item.bullets:
                    warnings.append(
                        GuardWarning(
                            bullet_source_id=b.source_id,
                            rewritten_text=b.rewritten_text,
                            reason=f"container source_id {item.source_id!r} not in master",
                        )
                    )
                    dropped.append(b.source_id)
                continue

            cleaned_bullets: List[TailoredBullet] = []
            for b in item.bullets:
                if b.source_id not in valid_bullet_ids:
                    warnings.append(
                        GuardWarning(
                            bullet_source_id=b.source_id,
                            rewritten_text=b.rewritten_text,
                            reason="bullet source_id not in master",
                        )
                    )
                    dropped.append(b.source_id)
                    continue

                src_bullet = master.bullet_by_id(b.source_id)
                assert src_bullet is not None  # we just checked it's in valid_bullet_ids
                # Variants are alternate phrasings authored by the candidate —
                # everything in them is legal source vocabulary.
                src_text_combined = "\n".join(src_bullet.all_source_texts())
                reason = _check_bullet(
                    b.rewritten_text, src_text_combined, jd_vocab, pointer_vocab
                )
                if reason:
                    warnings.append(
                        GuardWarning(
                            bullet_source_id=b.source_id,
                            rewritten_text=b.rewritten_text,
                            reason=reason,
                        )
                    )
                    dropped.append(b.source_id)
                    continue

                cleaned_bullets.append(b)

            if cleaned_bullets:
                cleaned_items.append(
                    TailoredItem(source_id=item.source_id, bullets=cleaned_bullets)
                )

        if cleaned_items:
            cleaned_sections.append(
                TailoredSection(name=section.name, items=cleaned_items)
            )

    # Summary check: cheaper guard — if it has rogue numbers, drop the summary
    cleaned_summary = tailored.summary
    if cleaned_summary:
        # No specific source for the summary — allow numbers ONLY if they appear
        # somewhere in the master text. Build a permissive whitelist.
        master_numbers = _extract_numbers(master.summary or "")
        for exp in master.experience:
            for b in exp.bullets:
                for src_text in b.all_source_texts():
                    master_numbers |= _extract_numbers(src_text)
        for proj in master.projects:
            for b in proj.bullets:
                for src_text in b.all_source_texts():
                    master_numbers |= _extract_numbers(src_text)
        summary_numbers = _extract_numbers(cleaned_summary)
        rogue = summary_numbers - master_numbers
        if rogue:
            warnings.append(
                GuardWarning(
                    bullet_source_id="<summary>",
                    rewritten_text=cleaned_summary,
                    reason=f"summary introduced number(s) not anywhere in master: {sorted(rogue)}",
                )
            )
            cleaned_summary = master.summary  # fall back to master summary

    cleaned = TailoredResume(
        summary=cleaned_summary,
        sections=cleaned_sections,
        dropped_source_ids=tailored.dropped_source_ids + dropped,
        rationale=tailored.rationale,
    )
    return GuardResult(cleaned=cleaned, warnings=warnings, dropped_bullet_ids=dropped)
