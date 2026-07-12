"""LLM-driven extraction of discrete accomplishments from raw brain-dump notes.

The wizard's Phase-1 brain-dump produces one chunk of free-text per
time-period. This module turns those notes into a list of structured
``DraftAccomplishment``s — each one a Bock-format bullet with the same
anti-fabrication guarantee as the resume tailor.

Anti-fabrication invariant
--------------------------

Numbers and proper nouns in a draft bullet MUST appear in the chunk's
``raw_notes`` OR be marked as literal placeholders ``[NUMBER]``,
``[METHOD]``, ``[TIMEFRAME]``, ``[SCOPE]``. ``validate_draft`` enforces
this by reusing the same regex helpers the resume guard uses, so the
moat is identical for the wizard.

The function returns the raw LLM response alongside the parsed drafts so
the UI's "Show LLM call" transparency toggle can surface what the model
actually said.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Iterable, List, Optional, Tuple

from .bock_tier import classify_bullet
from .guard import _extract_numbers, _extract_proper_nouns
from .llm import LLMProvider
from .session_store import BUCKETS, DraftAccomplishment, TimeChunk


# A chunk must have at least this much content before we'll bother calling
# the LLM. Below this we surface a "type a bit more here first" hint.
MIN_CHUNK_CHARS = 80

# Placeholder tokens the model is told it MAY use when content is missing.
# Stripped before fabrication checks so [NUMBER] does not register as a
# missing number; the wizard surfaces these to the user in Phase 5 (polish).
_PLACEHOLDERS = ("[NUMBER]", "[METHOD]", "[TIMEFRAME]", "[SCOPE]")
_PLACEHOLDER_RE = re.compile(
    r"\[(?:NUMBER|METHOD|TIMEFRAME|SCOPE)\]",
)


# ---------- system prompt ----------

SYSTEM_PROMPT = """\
You are an honest, careful resume coach. The user has dumped raw, stream-of-consciousness
notes about one period of their career. Your job is to turn those notes into a list of
discrete, recruiter-ready accomplishments — each in Laszlo Bock's "XYZ" form
("Accomplished X as measured by Y by doing Z").

NON-NEGOTIABLE RULES
====================

1. DO NOT INVENT. Never introduce numbers, percentages, dollar amounts, time durations,
   tool names, company names, team sizes, customer counts, or any other concrete claim
   that does not appear in the user's raw notes. Fabricating a single metric ruins the
   user's credibility if a recruiter probes.

2. When a Bock-format bullet needs a piece the user did not provide, use EXACTLY one of
   these literal placeholder tokens:
     [NUMBER]    — for missing metrics / percentages / dollars / counts
     [METHOD]    — for the "how" (Z) when the user described the outcome but not the path
     [TIMEFRAME] — for missing durations / dates / quarters
     [SCOPE]     — for missing team size / customer count / org reach
   Do NOT invent a fallback number ("about 30%") or guess a tool name — emit the
   placeholder instead. The user will fill them in later.

3. Every draft must include a `raw_quote` field: a verbatim substring of the user's
   notes (1-3 sentences) that grounds the draft. The substring MUST be copy-pasted from
   the notes, not paraphrased.

4. Prefer 3-6 distinct accomplishments per chunk. Combine repeated mentions of the same
   accomplishment into one. Skip filler ("had a great quarter").

5. Bullets should start with a strong action verb. Avoid weak openers
   ("Responsible for", "Helped with", "Worked on", "Member of").

OUTPUT FORMAT
=============

Return JSON only — no prose, no Markdown fences. Schema:

{
  "drafts": [
    {
      "raw_quote": "<verbatim substring of the user's notes>",
      "draft_bullet": "<Bock-format bullet, may contain [NUMBER], [METHOD], etc.>",
      "impact_score_hint": 1-5,
      "tags_hint": ["<short>", "<keywords>"]
    },
    ...
  ]
}

Nothing else. If the notes contain no discrete accomplishments, return {"drafts": []}.
"""


# ---------- public API ----------


class ExtractError(RuntimeError):
    """Raised when the LLM returns something we can't parse into drafts."""


def too_short(chunk: TimeChunk) -> bool:
    """Return True when the chunk has less than ``MIN_CHUNK_CHARS`` of real notes."""
    return len((chunk.raw_notes or "").strip()) < MIN_CHUNK_CHARS


def extract_drafts(
    chunk: TimeChunk,
    provider: LLMProvider,
    *,
    role_family: Optional[str] = None,
    role_family_other: Optional[str] = None,
    model: Optional[str] = None,
    timeout_s: int = 180,
) -> Tuple[List[DraftAccomplishment], str, str]:
    """Run the LLM extract for one chunk.

    Returns ``(drafts, user_prompt, raw_response)``. The user_prompt + raw
    response are returned so the wizard's "Show LLM call" toggle can render
    them; the drafts are pydantic-validated and tier-classified.
    """
    if too_short(chunk):
        raise ExtractError(
            f"chunk has only {len((chunk.raw_notes or '').strip())} chars; "
            f"need at least {MIN_CHUNK_CHARS}"
        )

    user_msg = _build_user_message(chunk, role_family, role_family_other)
    raw_response = provider.complete(
        SYSTEM_PROMPT, user_msg, model=model, timeout_s=timeout_s,
    )
    payload = _parse_response(raw_response)

    drafts: List[DraftAccomplishment] = []
    for item in payload.get("drafts", []):
        d = _draft_from_payload(item, chunk_id=chunk.id)
        if d is not None:
            drafts.append(d)
    return drafts, user_msg, raw_response


def validate_draft(draft: DraftAccomplishment) -> List[str]:
    """Return a list of fabrication reasons — empty list means clean.

    Authorised vocabulary = the draft's own ``raw_quote`` + every
    user_followup recorded during polish + placeholder tokens. The
    draft is checked against the union via the existing guard regexes.
    """
    legal_sources = [draft.raw_quote, *draft.user_followups]
    legal_blob = "\n".join(legal_sources)

    text_to_check = _strip_placeholders(draft.draft_bullet)

    reasons: List[str] = []

    rogue_numbers = _extract_numbers(text_to_check) - _extract_numbers(legal_blob)
    if rogue_numbers:
        reasons.append(
            f"introduced number(s) not grounded in raw notes: {sorted(rogue_numbers)}"
        )

    rogue_nouns = (
        _extract_proper_nouns(text_to_check) - _extract_proper_nouns(legal_blob)
    )
    if rogue_nouns:
        reasons.append(
            f"introduced proper noun(s) not grounded in raw notes: {sorted(rogue_nouns)}"
        )
    return reasons


# ---------- internals ----------


def _strip_placeholders(text: str) -> str:
    """Remove placeholder tokens so the fabrication guard doesn't flag them."""
    return _PLACEHOLDER_RE.sub("", text)


def _build_user_message(
    chunk: TimeChunk,
    role_family: Optional[str],
    role_family_other: Optional[str],
) -> str:
    """Assemble the user message handed to the LLM."""
    role_blurb = ""
    if role_family and role_family != "other":
        role_blurb = f"\nUser's role family: {role_family}\n"
    elif role_family == "other" and role_family_other:
        role_blurb = f"\nUser's role (in their words): {role_family_other}\n"

    return (
        f"Chunk label: {chunk.label}\n"
        f"Chunk period: {chunk.start} to {chunk.end}\n"
        f"{role_blurb}\n"
        f"--- RAW NOTES (verbatim, do not infer beyond them) ---\n"
        f"{(chunk.raw_notes or '').strip()}\n"
        f"--- END NOTES ---\n\n"
        f"Return JSON only. No prose, no Markdown fences."
    )


def _parse_response(raw: str) -> dict:
    """Parse the LLM's reply. Tolerates code fences and surrounding prose."""
    text = raw.strip()
    # Strip code fences if present.
    fence_match = re.match(r"^```(?:json)?\s*\n(.*)\n```$", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()
    # Fall back to first-JSON-object scan if there's stray prose.
    if not text.startswith("{"):
        start = text.find("{")
        if start == -1:
            raise ExtractError("LLM response did not contain a JSON object")
        text = text[start:]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as e:
        raise ExtractError(f"LLM JSON parse failed: {e}") from e
    if not isinstance(payload, dict) or "drafts" not in payload:
        raise ExtractError("LLM response missing top-level `drafts` array")
    if not isinstance(payload["drafts"], list):
        raise ExtractError("LLM `drafts` field must be an array")
    return payload


def _draft_from_payload(item: dict, *, chunk_id: str) -> Optional[DraftAccomplishment]:
    """Build a DraftAccomplishment from one LLM payload entry.

    Returns ``None`` for entries that are missing required fields; the
    surrounding extract call simply skips those rather than failing the
    whole batch.
    """
    raw_quote = (item.get("raw_quote") or "").strip()
    draft_bullet = (item.get("draft_bullet") or "").strip()
    if not raw_quote or not draft_bullet:
        return None

    tier, missing = classify_bullet(draft_bullet)

    # `impact_score_hint` is optional and may be omitted, None, or out of range.
    raw_hint = item.get("impact_score_hint")
    hint: Optional[int] = None
    if isinstance(raw_hint, int) and 1 <= raw_hint <= 5:
        hint = raw_hint

    tags = item.get("tags_hint") or []
    if not isinstance(tags, list):
        tags = []
    tags = [str(t).strip() for t in tags if isinstance(t, (str, int, float)) and str(t).strip()]

    return DraftAccomplishment(
        id=f"draft-{uuid.uuid4().hex[:8]}",
        chunk_id=chunk_id,
        raw_quote=raw_quote,
        draft_bullet=draft_bullet,
        tier=tier,
        missing=missing,
        impact_score_hint=hint,
        tags_hint=tags,
        user_confirmed=False,
    )


# ---------- merge helper for re-extraction ----------


def merge_drafts_preserving_confirmed(
    existing: Iterable[DraftAccomplishment],
    fresh: Iterable[DraftAccomplishment],
) -> List[DraftAccomplishment]:
    """Warn-then-replace policy: confirmed drafts survive; the rest are replaced.

    Used by the wizard's re-extract flow. Confirmed drafts come first
    (they're the user's curated picks), followed by the freshly extracted
    set in source order.
    """
    confirmed = [d for d in existing if d.user_confirmed]
    return [*confirmed, *fresh]


# ---------- categorize (Phase 3) ----------


CATEGORIZE_SYSTEM_PROMPT = """\
You sort discrete accomplishment bullets into one of the canonical resume
sections used by this tool. You DO NOT rewrite or grade the bullets — only
slot each one into the best-fitting section.

The 7 canonical sections (use these exact ids):

  experience       — paid roles, internships, formal jobs (incl. consulting engagements)
  projects         — side projects, open-source contributions, hackathons, personal builds
  education        — degrees, university coursework, dissertations, theses, defended research
  extracurricular  — clubs, student government, volunteering, sports, community organising
  skills           — *only* if the bullet is purely a skill ("Fluent in Python"); rare
  awards           — distinctions, scholarships, prizes, named recognitions
  certifications   — completed coursework with a credential (AWS, CFA L1, Coursera cert, etc.)

Rules:
1. Prefer `experience` for anything that happened inside a paid role. A bullet
   that *describes* a project shipped during a job goes in `experience`, not `projects`.
2. `projects` is for things the user built on their own time or as
   freelancers / open-source — outside the scope of a job description.
3. `education` is for academic accomplishments. Awards from school go in
   `awards` unless they are inseparable from the degree.
4. `skills` is intentionally narrow. A bullet that demonstrates a skill via
   work goes in `experience` or `projects`; only put pure skill claims here.
5. If a bullet truly does not fit any bucket, pick the closest reasonable
   match and lower your confidence to 1-2.

Return JSON only — no prose, no Markdown fences. Schema:

{
  "assignments": [
    {"draft_id": "<id>", "bucket": "<one of the 7 ids>", "confidence": 1-5, "rationale": "<one short clause>"}
  ]
}

If no drafts are sent, return {"assignments": []}.
"""


def categorize_drafts(
    drafts: List[DraftAccomplishment],
    provider: LLMProvider,
    *,
    role_family: Optional[str] = None,
    role_family_other: Optional[str] = None,
    model: Optional[str] = None,
    timeout_s: int = 180,
) -> Tuple[dict, str, str]:
    """Run the LLM categorizer over the drafts that don't yet have a bucket.

    Returns ``(assignments, user_prompt, raw_response)`` where assignments
    is ``{draft_id: {"bucket": str, "confidence": int, "rationale": str}}``.
    The caller applies the bucket strings to ``DraftAccomplishment.bucket``.

    Already-bucketed drafts are passed through untouched (idempotent). Pass
    every draft (bucketed or not) in for context; the function decides
    which ones to send to the model.
    """
    needs_assignment = [d for d in drafts if not d.bucket]
    if not needs_assignment:
        return {}, "", ""

    user_msg = _build_categorize_message(
        needs_assignment, role_family, role_family_other,
    )
    raw_response = provider.complete(
        CATEGORIZE_SYSTEM_PROMPT, user_msg, model=model, timeout_s=timeout_s,
    )
    payload = _parse_categorize_response(raw_response)

    valid_buckets = set(BUCKETS)
    assignments: dict = {}
    for item in payload.get("assignments", []):
        did = item.get("draft_id")
        bucket = item.get("bucket")
        if did is None or bucket not in valid_buckets:
            continue
        assignments[did] = {
            "bucket": bucket,
            "confidence": _coerce_confidence(item.get("confidence")),
            "rationale": str(item.get("rationale") or "").strip(),
        }
    return assignments, user_msg, raw_response


def _build_categorize_message(
    drafts: List[DraftAccomplishment],
    role_family: Optional[str],
    role_family_other: Optional[str],
) -> str:
    role_blurb = ""
    if role_family and role_family != "other":
        role_blurb = f"User's role family: {role_family}\n"
    elif role_family == "other" and role_family_other:
        role_blurb = f"User's role (in their words): {role_family_other}\n"

    lines = [
        role_blurb,
        f"Number of drafts to categorize: {len(drafts)}",
        "",
        "Drafts:",
    ]
    for d in drafts:
        tags = ", ".join(d.tags_hint) if d.tags_hint else "<none>"
        lines.append(
            f"- id={d.id} | bullet={d.draft_bullet!r} | tags={tags}"
        )
    lines.append("")
    lines.append("Return JSON only.")
    return "\n".join(lines)


def _parse_categorize_response(raw: str) -> dict:
    text = raw.strip()
    fence_match = re.match(r"^```(?:json)?\s*\n(.*)\n```$", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()
    if not text.startswith("{"):
        start = text.find("{")
        if start == -1:
            raise ExtractError("categorize: response did not contain a JSON object")
        text = text[start:]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as e:
        raise ExtractError(f"categorize: JSON parse failed: {e}") from e
    if not isinstance(payload, dict) or "assignments" not in payload:
        raise ExtractError("categorize: response missing `assignments` array")
    if not isinstance(payload["assignments"], list):
        raise ExtractError("categorize: `assignments` field must be an array")
    return payload


def _coerce_confidence(value) -> int:
    """Force confidence into the 1-5 band. Bad values default to 3 (neutral)."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 3
    if n < 1:
        return 1
    if n > 5:
        return 5
    return n


# ---------- merge (Phase 3) ----------


def merge_two_drafts(
    a: DraftAccomplishment, b: DraftAccomplishment,
) -> DraftAccomplishment:
    """Structurally fuse two drafts into one new draft.

    The merged draft carries provenance from both: ``raw_quote`` becomes
    the two original quotes separated by a clear divider, ``user_followups``
    are concatenated, ``tags_hint`` are unioned, and the bullet text is
    placeholder-joined so the user edits the final phrasing themselves.

    The new draft inherits the *higher* impact_score_hint and the *better*
    Bock tier of the two. ``user_confirmed`` resets to ``False`` because
    the user must approve the merged result.
    """
    merged_bullet = f"{a.draft_bullet}\n— combined with —\n{b.draft_bullet}"
    merged_quote = f"{a.raw_quote}\n— combined with —\n{b.raw_quote}"

    # Union of tags, preserving order from a then b.
    seen: set = set()
    tags: List[str] = []
    for t in [*a.tags_hint, *b.tags_hint]:
        key = t.lower()
        if key not in seen:
            seen.add(key)
            tags.append(t)

    tier, missing = classify_bullet(merged_bullet)

    return DraftAccomplishment(
        id=f"draft-{uuid.uuid4().hex[:8]}",
        chunk_id=a.chunk_id,  # keep the earlier chunk's id as canonical
        raw_quote=merged_quote,
        draft_bullet=merged_bullet,
        tier=tier,
        missing=missing,
        impact_score_hint=_max_optional(a.impact_score_hint, b.impact_score_hint),
        tags_hint=tags,
        bucket=a.bucket or b.bucket,
        user_confirmed=False,
        where_to_look=list({*a.where_to_look, *b.where_to_look}),
        user_followups=[*a.user_followups, *b.user_followups],
    )


def _max_optional(a: Optional[int], b: Optional[int]) -> Optional[int]:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


# ---------- polish (Phase 5: XYZ Awesome) ----------


# Bock Part 02 + 04 — when the user can't recall a metric, here's where
# to look. Surfaced in the wizard's polish pane next to the y_metric input.
WHERE_TO_LOOK: dict[str, list[str]] = {
    "y_metric": [
        "Performance reviews / self-assessments from that period",
        "OKR / KPI sheets — the goal you set, and the final number",
        "Slack DMs and channel posts where you celebrated the win",
        "Pull request titles + commit messages from that month",
        "Calendar from that month — meetings often reveal the project shape",
        "Old emails — search 'launched', 'shipped', 'closed', 'won', 'reduced'",
        "Your manager (or a former teammate) — ask: 'what number do you remember about X?'",
        "Public artefacts — blog posts, conference talks, press releases",
    ],
    "z_method": [
        "What change did you make that the team hadn't tried before?",
        "What tool, framework, or process did you introduce?",
        "Who did you collaborate with — what was your specific contribution?",
        "What constraint forced the approach?",
    ],
    "x_strong_verb": [
        "Started with 'Responsible for' / 'Helped' / 'Worked on'? Pick from:",
        "Led / Owned / Designed / Built / Shipped / Launched / Drove / Scaled",
        "Reduced / Cut / Eliminated / Recovered / Restored",
        "Mentored / Hired / Onboarded / Coached / Unblocked",
        "Negotiated / Closed / Won / Secured / Renewed",
    ],
}


POLISH_SYSTEM_PROMPT = """\
You are a careful resume editor. The user wrote a draft Bock-format bullet with
placeholder tokens marking gaps ([NUMBER], [METHOD], [TIMEFRAME], [SCOPE]).
They have now provided answers for some or all of those gaps. Produce a polished
final bullet by SUBSTITUTING the user's answers into the matching placeholders.

NON-NEGOTIABLE RULES
====================

1. DO NOT INVENT. Never introduce numbers, percentages, dollar amounts, time
   durations, tool names, company names, or proper nouns that don't appear in
   either the original draft, its raw_quote, or the user's follow-up answers.
2. DO NOT add details the user didn't provide. If a placeholder still has no
   answer, KEEP the placeholder token in the polished output exactly as it
   appears (e.g. [NUMBER]).
3. Bullets must start with a strong action verb. If the original opens with
   a weak phrase ("Responsible for", "Helped with", "Worked on"), replace it
   with the strongest verb consistent with what the user is actually claiming —
   but only if the user supplied an x_strong_verb answer or a clear claim that
   warrants the upgrade.
4. Aim for Bock's "Awesome" tier: action verb + measurable outcome + how-clause.
   Don't fabricate to get there. If the bullet still has placeholders, that is
   fine — Bock's "Better" tier with one honest placeholder beats a fake "Awesome".
5. Tighten phrasing. Drop filler. One sentence per bullet.

OUTPUT FORMAT
=============

Return JSON only — no prose, no Markdown fences. Schema:

{
  "polished_bullet": "<the rewritten bullet text>",
  "rationale": "<one short sentence describing what changed>"
}

Nothing else.
"""


class PolishError(RuntimeError):
    """Raised when polish output is malformed OR introduces fabrications."""


def polish_draft(
    draft: DraftAccomplishment,
    followups: dict,
    provider: LLMProvider,
    *,
    model: Optional[str] = None,
    timeout_s: int = 180,
) -> Tuple[DraftAccomplishment, str, str, List[str]]:
    """Polish a draft using user-supplied follow-up answers.

    ``followups`` keys come from ``DraftAccomplishment.missing``:
    ``y_metric`` / ``z_method`` / ``x_strong_verb``. Values are short strings.

    Returns ``(updated_draft, user_prompt, raw_response, fabrication_warnings)``.

    The LLM is instructed to **substitute** the user's answers into the
    draft's placeholders, never invent. The result is run through
    :func:`validate_draft` against the union of raw_quote + every recorded
    user_followup; any rogue numbers or proper nouns surface as warnings.
    The caller decides whether to keep the polish or have the user retry.
    """
    user_msg = _build_polish_message(draft, followups)
    raw_response = provider.complete(
        POLISH_SYSTEM_PROMPT, user_msg, model=model, timeout_s=timeout_s,
    )

    payload = _parse_polish_response(raw_response)
    polished_bullet = (payload.get("polished_bullet") or "").strip()
    if not polished_bullet:
        raise PolishError("LLM polish response missing `polished_bullet`")

    # Append each non-empty followup to user_followups so the guard treats
    # them as legal source vocabulary on this and any future polish run.
    new_followups = list(draft.user_followups)
    for key in ("y_metric", "z_method", "x_strong_verb"):
        answer = (followups.get(key) or "").strip()
        if answer:
            new_followups.append(answer)

    tier, missing = classify_bullet(polished_bullet)

    polished = DraftAccomplishment(
        id=draft.id,
        chunk_id=draft.chunk_id,
        raw_quote=draft.raw_quote,
        draft_bullet=polished_bullet,
        tier=tier,
        missing=missing,
        impact_score_hint=draft.impact_score_hint,
        tags_hint=draft.tags_hint,
        bucket=draft.bucket,
        user_confirmed=draft.user_confirmed,
        where_to_look=draft.where_to_look,
        user_followups=new_followups,
    )

    fabrication_warnings = validate_draft(polished)
    return polished, user_msg, raw_response, fabrication_warnings


def _build_polish_message(draft: DraftAccomplishment, followups: dict) -> str:
    """Compose the user message for the polish call."""

    def _line(key: str, label: str) -> str:
        ans = (followups.get(key) or "").strip()
        if ans:
            return f"- {label}: {ans}"
        return f"- {label}: <user did not answer — keep placeholder if any>"

    return (
        f"Original draft bullet:\n{draft.draft_bullet}\n\n"
        f"Raw quote (the source-of-truth from the user's notes):\n"
        f"{draft.raw_quote}\n\n"
        f"Tier classifier currently reports: {draft.tier} "
        f"(missing: {', '.join(draft.missing) if draft.missing else 'nothing'})\n\n"
        f"User's follow-up answers — substitute these in:\n"
        f"{_line('y_metric', 'y_metric (the missing number / metric)')}\n"
        f"{_line('z_method', 'z_method (the missing how-clause)')}\n"
        f"{_line('x_strong_verb', 'x_strong_verb (a stronger opener)')}\n\n"
        f"Return JSON only."
    )


def _parse_polish_response(raw: str) -> dict:
    """Parse the polish LLM reply. Same tolerance as extract / categorize."""
    text = raw.strip()
    fence_match = re.match(r"^```(?:json)?\s*\n(.*)\n```$", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()
    if not text.startswith("{"):
        start = text.find("{")
        if start == -1:
            raise PolishError("polish: response did not contain a JSON object")
        text = text[start:]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as e:
        raise PolishError(f"polish: JSON parse failed: {e}") from e
    if not isinstance(payload, dict):
        raise PolishError("polish: response must be a JSON object")
    return payload
