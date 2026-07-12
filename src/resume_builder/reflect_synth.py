"""Synthesis pass for the Four Levers self-reflection worksheet.

The worksheet (``templates/reflect.html``) collects the user's own answers
across four levers: Judgment, Pressure, Trust, and Signal & Skills. This
module turns those answers into:

- ``edge_summary``       — 1-paragraph statement of the candidate's edge,
                           in their own words, drawing only from what they
                           wrote.
- ``next_steps``         — 3-5 concrete actions (roles to look for, types
                           of conversations to start, skills to surface).
- ``master_additions``   — bullets they should add to ``master.yaml`` that
                           they're currently under-showing.
- ``linkedin_additions`` — what to surface on LinkedIn (Headline / About /
                           Featured tweaks) that the worksheet revealed.
- ``rationale``          — 1-2 sentence why-this-pattern explanation.

Anti-fabrication
----------------

Unlike the resume tailor, the user is the sole source of truth here. The
LLM is forbidden from inventing companies, role titles, products, or
named claims the user didn't write. Generic English ("ownership",
"systems thinking", "stakeholder communication") is allowed because it's
descriptive vocabulary, not invented credentials.

The guard ``validate_synthesis`` extracts proper nouns from the model
response and rejects any that don't appear in the user's worksheet
answers. Numbers get the same treatment — though numeric claims are rare
in this worksheet.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .guard import (
    _COMMON_STARTERS,
    _PROPER_NOUN_RE,
    _extract_numbers,
    _extract_proper_nouns,
)
from .llm import LLMError, LLMProvider
from .tailor import _extract_first_json_object


# The set of answer keys the worksheet writes. Used to validate the input
# the route handler receives and to build the user message in a stable
# order so the LLM sees a predictable shape.
LEVER_KEYS: Dict[str, List[str]] = {
    "judgment": [
        "l1-p1", "l1-p2", "l1-p3", "l1-p4", "l1-p5",
        "l1-s1", "l1-s2",
    ],
    "pressure": [
        "l2-p1", "l2-p2", "l2-p3", "l2-p4", "l2-p5",
        "l2-s1", "l2-s2",
    ],
    "trust": [
        "l3-p1", "l3-p2", "l3-p3", "l3-p4", "l3-p5",
        "l3-s1", "l3-s2",
    ],
    "signal_skills": [
        "l4-p1", "l4-p2", "l4-p3", "l4-p4", "l4-p5",
        "l4-p6", "l4-p7", "l4-p8", "l4-p9",
        "l4-s1", "l4-s2",
    ],
}


# A response with fewer than this many non-blank answers isn't worth
# synthesising — the LLM won't have enough material to pattern, and the
# result would be padded fluff. The UI also blocks at the same floor.
MIN_FILLED_ANSWERS = 4


class ReflectSynthError(LLMError):
    """Raised when the LLM returns something we can't parse into the synthesis shape."""


# ---------- system prompt ----------


SYSTEM_PROMPT = """\
You are a career coach reading a candidate's self-reflection worksheet.
The worksheet has four sections (Judgment / Pressure / Trust / Signal &
Skills) — together "The Four Levers of Your Edge." Your job: pattern the
candidate's own words into a 1-page summary that helps them find their
next role.

HARD RULES — VIOLATING THESE IS A BUG:

1. NEVER invent a company name, product name, role title, technology, or
   named claim the candidate did not write.
2. NEVER invent numbers, percentages, dollar amounts, durations, or team
   sizes the candidate did not write.
3. Generic English vocabulary IS allowed ("ownership", "systems thinking",
   "stakeholder communication", "writing", "code review"). What's
   forbidden is specifics that aren't grounded in the worksheet.
4. If the candidate left a section blank or thin, say so plainly in the
   rationale — DO NOT pad the missing material with invented narrative.
5. Your job is to PATTERN, not to FABRICATE. If you find yourself
   inventing a story, stop and write a shorter, truer summary instead.

STYLE:

- Edge summary is 2-4 sentences max. Concrete, not generic.
- Next steps are imperative ("Apply to <role-archetype>", "Ask <kind of
  person> for a coffee chat"). 3-5 items.
- Master additions are bullet ideas the candidate could add to their
  resume — phrased as the bullet would read, drawing on the worksheet
  language.
- LinkedIn additions are specific section-level tweaks
  ("Add to your About: ...", "Update headline to mention ...").

OUTPUT: Return ONLY valid JSON conforming to the schema below. No prose
before or after. No markdown fences."""


JSON_SCHEMA_HINT = """{
  "edge_summary": "<2-4 sentence statement of the candidate's edge, in their own words>",
  "next_steps": [
    "<concrete imperative action 1>",
    "<concrete imperative action 2>",
    "<3-5 total>"
  ],
  "master_additions": [
    "<bullet idea to add to master.yaml — phrased like the bullet would read>",
    "<...>"
  ],
  "linkedin_additions": [
    "<section-level LinkedIn tweak: 'Add to your About: ...' / 'Update headline ...' etc.>",
    "<...>"
  ],
  "rationale": "<1-2 sentence why this pattern>"
}"""


# ---------- result types ----------


@dataclass
class SynthesisResult:
    edge_summary: str
    next_steps: List[str] = field(default_factory=list)
    master_additions: List[str] = field(default_factory=list)
    linkedin_additions: List[str] = field(default_factory=list)
    rationale: str = ""


@dataclass
class SynthesisWarning:
    section: str  # "edge_summary" / "next_steps[2]" / etc.
    reason: str
    text: str


@dataclass
class SynthesisGuardResult:
    cleaned: SynthesisResult
    warnings: List[SynthesisWarning] = field(default_factory=list)


# ---------- user message ----------


_LABELS: Dict[str, str] = {
    # Lever 1 — Judgment
    "l1-p1": "When did I say no — and I was right?",
    "l1-p2": "Where do I cut through noise faster than others?",
    "l1-p3": "What trade-offs do people trust me to call?",
    "l1-p4": "What messes have I helped avoid — that others didn't see coming?",
    "l1-p5": "What decisions do I quietly shape — even if I'm not the one announcing them?",
    "l1-s1": "I bring judgment when ___",
    "l1-s2": "and it helps people avoid ___",
    # Lever 2 — Pressure
    "l2-p1": "When did I help move something forward when everyone else was reacting?",
    "l2-p2": "What do I usually step into when things get messy?",
    "l2-p3": "What kind of chaos do I handle better than most?",
    "l2-p4": "Who pulls me in when they're stuck?",
    "l2-p5": "What do I keep moving — even when things feel blocked?",
    "l2-s1": "When things get messy, I step in to ___",
    "l2-s2": "because I stay steady when ___",
    # Lever 3 — Trust
    "l3-p1": "What do people always expect me to handle — even if it's unspoken?",
    "l3-p2": "What would quietly fall apart if I stopped doing something I always do?",
    "l3-p3": "What type of work or conversations do people trust me with?",
    "l3-p4": "What do I carry that others would struggle to name — but know they need?",
    "l3-p5": "What feedback or phrase about how I work has stuck with me?",
    "l3-s1": "Even when no one says it, people rely on me to ___",
    "l3-s2": "and I carry that with ___",
    # Lever 4 — Signal & Skills
    "l4-p1": "What hard skills do I use often, but don't talk about enough?",
    "l4-p2": "What soft skills am I showing daily, but rarely name?",
    "l4-p3": "What business context do I bring that others overlook?",
    "l4-p4": "What industry patterns or user behaviors do I understand well — but take for granted?",
    "l4-p5": "If someone had to pitch what I bring — would they get it right?",
    "l4-p6": "Where is my edge clear only to people who work directly with me?",
    "l4-p7": "What have I done that says a lot — but I've never shared?",
    "l4-p8": "If I left today, what part of my value wouldn't carry over because it's invisible?",
    "l4-p9": "What version of me are people still seeing — that I've already outgrown?",
    "l4-s1": "My edge is ___",
    "l4-s2": "but I've been underselling it by ___",
}


_LEVER_TITLES: Dict[str, str] = {
    "judgment": "1. Judgment — How you decide what matters",
    "pressure": "2. Pressure — How you show up when things break",
    "trust": "3. Trust — What people rely on you for",
    "signal_skills": "4. Signal & Skills — Whether others can see your edge",
}


def filled_answer_count(answers: Dict[str, str]) -> int:
    """Number of non-blank answers across all levers."""
    return sum(1 for v in answers.values() if (v or "").strip())


def build_user_message(answers: Dict[str, str]) -> str:
    """Assemble the user-message body in a stable per-lever order."""
    parts: List[str] = ["# Four Levers worksheet — candidate's answers\n"]
    for lever_id, keys in LEVER_KEYS.items():
        parts.append(f"\n## {_LEVER_TITLES[lever_id]}\n")
        any_filled = False
        for k in keys:
            v = (answers.get(k) or "").strip()
            label = _LABELS.get(k, k)
            if v:
                parts.append(f"- **{label}**\n  {v}\n")
                any_filled = True
        if not any_filled:
            parts.append("_(candidate left this lever blank)_\n")
    parts.append("\n# Output schema (return ONLY this JSON, no prose, no fences)\n\n")
    parts.append(JSON_SCHEMA_HINT)
    return "".join(parts)


# ---------- parse + validate ----------


def parse_response_text(text: str) -> SynthesisResult:
    """Pull a SynthesisResult out of the LLM reply. Tolerant of fences/prose.

    All parse failures are re-raised as ``ReflectSynthError`` so callers
    have a single exception type to handle (matching the resume tailor's
    ``ClaudeCliError`` pattern).
    """
    try:
        payload_text = _extract_first_json_object(text)
    except ValueError as e:
        raise ReflectSynthError(
            f"Synthesis: no JSON object found in response. First 500 chars:\n{text[:500]}"
        ) from e
    try:
        data = json.loads(payload_text)
    except json.JSONDecodeError as e:
        raise ReflectSynthError(
            f"Synthesis: response was not valid JSON ({e}). First 500 chars:\n{text[:500]}"
        ) from e
    if not isinstance(data, dict):
        raise ReflectSynthError(
            f"Synthesis: expected JSON object, got {type(data).__name__}"
        )

    edge_summary = (data.get("edge_summary") or "").strip()
    if not edge_summary:
        raise ReflectSynthError("Synthesis missing edge_summary")

    def _str_list(key: str) -> List[str]:
        items = data.get(key) or []
        if not isinstance(items, list):
            return []
        return [str(x).strip() for x in items if str(x).strip()]

    return SynthesisResult(
        edge_summary=edge_summary,
        next_steps=_str_list("next_steps"),
        master_additions=_str_list("master_additions"),
        linkedin_additions=_str_list("linkedin_additions"),
        rationale=(data.get("rationale") or "").strip(),
    )


# ---------- anti-fabrication guard ----------


# Generic vocabulary that pattern-extractors flag as proper nouns but that
# aren't really named credentials. Same shape as the cover-letter allowlist
# but tuned for the career-coach idiom.
_REFLECT_ALLOWLIST: set[str] = {
    # Pronouns / sentence starters
    "i", "my", "we", "our", "your", "you", "yourself",
    # Universal English in coaching prose
    "the", "and", "or", "but", "for", "with", "from", "into", "of", "to",
    "ai", "api", "saas", "b2b", "b2c", "ic",  # common all-caps abbrevs
    # Career-archetype vocab (universal — not invented credentials)
    "engineer", "engineering", "engineers", "developer", "developers",
    "product", "products", "manager", "managers", "founder", "founders",
    "co-founder", "cofounder", "leader", "leadership", "designer",
    "designers", "researcher", "research", "analyst", "consultant",
    "consulting", "intern", "internship", "team", "teams", "company",
    "companies", "role", "roles", "career", "job", "jobs",
    "senior", "staff", "principal", "junior", "lead", "founding",
    "associate", "director", "vp", "head", "chief", "executive",
    "backend", "frontend", "fullstack", "full-stack", "infrastructure",
    "infra", "platform", "mobile", "security", "data", "ml",
    "devops", "sre", "qa", "growth", "marketing", "ops", "operations",
    "sales", "finance", "hr",
    # Generic skill / behavior vocab
    "writing", "communication", "ownership", "judgment", "mentorship",
    "coaching", "leadership", "strategy", "execution", "thinking",
    "systems", "design", "review", "documentation", "facilitation",
    # Letter-writing / coaching idiom
    "added", "added:", "headline", "about", "featured", "skills",
    "experience", "education", "linkedin", "github", "summary",
    "next", "step", "steps", "edge", "pattern",
    # Time / region vocab
    "monday", "tuesday", "wednesday", "thursday", "friday",
    "today", "tomorrow", "yesterday", "weekly", "monthly", "quarterly",
    "annual", "annually",
    # Common modal/verb sentence starters
    "apply", "ask", "share", "post", "draft", "send", "reach", "talk",
    "tell", "show", "surface", "highlight", "schedule", "join",
    "consider", "explore", "investigate", "list", "name", "write",
    "update", "polish", "rephrase", "rewrite", "drop", "add", "include",
    # Common bullet starters
    "led", "built", "shipped", "designed", "owned", "drove", "scaled",
    "improved", "ran", "wrote",
    # Bullet-verb-y common starters (handled by guard's _COMMON_STARTERS too,
    # but we duplicate for explicit allowlist behaviour)
    "the", "a", "an", "this", "that",
}


def _extract_proper_nouns_with_allowlist(text: str) -> set[str]:
    """Wrap guard._extract_proper_nouns (which handles sentence-start
    capitalization correctly) and additionally filter the reflect
    allowlist. Words like "Reset" at the start of a sentence get dropped
    by the underlying helper because they're capitalized verbs, not real
    proper nouns. The reflect allowlist then removes coaching-prose
    vocabulary that the underlying helper would flag as a real proper
    noun (e.g. acronyms like B2B).
    """
    return {
        token for token in _extract_proper_nouns(text)
        if token not in _REFLECT_ALLOWLIST
    }


def _build_worksheet_vocab(answers: Dict[str, str]) -> tuple[set[str], set[str]]:
    """Return ``(allowed_nouns, allowed_numbers)`` from the user's answers.

    Anything the user wrote is fair game in the LLM's output. Tokens are
    extracted with the same regex the resume guard uses so the matching
    logic stays consistent.
    """
    blob = "\n".join((v or "") for v in answers.values())
    allowed_nouns = _extract_proper_nouns_with_allowlist(blob)
    allowed_numbers = _extract_numbers(blob)
    return allowed_nouns, allowed_numbers


# Numbers up to 100 are treated as generic coaching-prose vocabulary
# ("ask three leaders", "send five emails", "schedule 30 minutes"). They
# don't represent invented credentials the way "scaled to 50M users" would
# in a resume bullet. The reflect synthesis is coaching advice, not CV
# claims — fabrication risk lives in proper nouns (company/product/tool
# names), which the proper-noun check still catches strictly.
_GENERIC_COUNT_RE = re.compile(r"^\d+(?:[\.,]\d+)?$")


def _is_generic_small_number(token: str) -> bool:
    """True for small whole numbers / time durations (≤100, no K/M/B
    magnitude suffix). These pass the reflect guard as generic vocab."""
    if not _GENERIC_COUNT_RE.match(token):
        # Has a magnitude/unit suffix (K/M/B/ms/s/x) — not generic.
        return False
    try:
        value = float(token.replace(",", ""))
    except ValueError:
        return False
    return value <= 100


def _check_string(
    text: str,
    allowed_nouns: set[str],
    allowed_numbers: set[str],
) -> Optional[str]:
    """Return failure reason if ``text`` introduces tokens not in the worksheet."""
    rogue_numbers = {
        n for n in _extract_numbers(text) - allowed_numbers
        if not _is_generic_small_number(n)
    }
    if rogue_numbers:
        return f"introduced number(s) not in worksheet: {sorted(rogue_numbers)}"
    rogue_nouns = _extract_proper_nouns_with_allowlist(text) - allowed_nouns
    if rogue_nouns:
        return f"introduced proper noun(s) not in worksheet: {sorted(rogue_nouns)}"
    return None


def validate_synthesis(
    answers: Dict[str, str],
    result: SynthesisResult,
) -> SynthesisGuardResult:
    """Run the anti-fabrication guard over every field of the synthesis.

    Failures are warnings, not drops — the user can re-prompt or accept
    the risk. (Following the cover-letter guard's UX pattern.)
    """
    allowed_nouns, allowed_numbers = _build_worksheet_vocab(answers)
    warnings: List[SynthesisWarning] = []

    def _check(field_name: str, text: str) -> None:
        if not text:
            return
        reason = _check_string(text, allowed_nouns, allowed_numbers)
        if reason:
            warnings.append(SynthesisWarning(
                section=field_name, reason=reason, text=text,
            ))

    _check("edge_summary", result.edge_summary)
    for i, step in enumerate(result.next_steps):
        _check(f"next_steps[{i}]", step)
    for i, add in enumerate(result.master_additions):
        _check(f"master_additions[{i}]", add)
    for i, add in enumerate(result.linkedin_additions):
        _check(f"linkedin_additions[{i}]", add)
    _check("rationale", result.rationale)

    return SynthesisGuardResult(cleaned=result, warnings=warnings)


# ---------- orchestrator ----------


def synthesize(
    answers: Dict[str, str],
    provider: LLMProvider,
    *,
    model: Optional[str] = None,
    timeout_s: int = 180,
) -> tuple[SynthesisResult, SynthesisGuardResult, str, str]:
    """Run the synthesis end-to-end.

    Returns ``(result, guard_result, user_message, raw_response)``. The
    user_message + raw response are returned for transparency (the UI can
    surface them in a "show LLM call" toggle the same way the wizard does).
    """
    if filled_answer_count(answers) < MIN_FILLED_ANSWERS:
        raise ReflectSynthError(
            f"Need at least {MIN_FILLED_ANSWERS} non-blank answers "
            f"to synthesize; got {filled_answer_count(answers)}."
        )
    user_msg = build_user_message(answers)
    raw_response = provider.complete(
        SYSTEM_PROMPT, user_msg, model=model, timeout_s=timeout_s,
    )
    result = parse_response_text(raw_response)
    guard = validate_synthesis(answers, result)
    return result, guard, user_msg, raw_response
