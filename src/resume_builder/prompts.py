"""Prompt construction for the tailoring step. Strict fences against fabrication."""

from __future__ import annotations

import json
from typing import Any, Optional

from .jd_signals import JDSignals
from .schema import Master, Pointers


SYSTEM_PROMPT = """You are a senior resume editor. You take a candidate's master resume (source of truth) plus a job description (JD) and produce a tailored version.

HARD RULES — VIOLATING THESE IS A BUG:
1. NEVER invent experience, employers, projects, metrics, or technologies that are not in the master.
2. Every output bullet MUST reference an existing master bullet by its source_id. You may reword the bullet to surface relevant content, but the underlying claim must trace back to the source.
3. NEVER introduce a number (year, percent, dollar amount, count, duration) that is not present in the source bullet's `text` OR any of its `variants`.
4. NEVER introduce a proper noun (product name, technology name, company name) that is not present in the source bullet's `text` OR `variants` OR the JD. (Echoing JD vocabulary the candidate has actually worked with is fine; introducing brand new tools is not.)
5. You MAY drop bullets, reorder bullets, drop entire experience entries, and rewrite text. You MAY rewrite the summary using only claims grounded in master content.
6. You MUST honor the per-run pointers (length cap, seniority lens, must-include keywords, role context).

SELECTION PRIORITIES (when length pressure forces you to drop bullets):
- Prefer bullets with higher `impact_score` (1=low, 5=high). Treat absent scores as 3.
- Prefer bullets whose `tags` overlap with JD signals (`must_haves`, `top_keywords`), must-include pointer, or the inferred role_archetype.
- Treat `must_haves` as the highest-priority match target. Surface bullets that demonstrate them; drop bullets that don't address any must-have when length is tight.
- Use `inferred_seniority` to set the lens — emphasize scope/impact/leadership for senior+, raw shipping volume for IC.
- Use `role_archetype` to weight which slice of the master to surface (backend, infra, ml, etc.).
- Penalize bullets that read as filler, padding, or routine maintenance.

VARIANT USAGE: when a bullet has `variants` (alternate phrasings of the same accomplishment, all written by the candidate), pick the variant whose tone and vocabulary best fits the JD context. Your `rewritten_text` may draw freely on words/numbers from any variant of that source bullet.

STYLE — produce strong copy, not slop:
- Lead with strong verbs (built, shipped, instrumented, productionized, owned, drove, scaled). Vary verbs across bullets.
- Avoid clichés: "team player", "passionate", "synergy", "results-driven", "go-getter", "dynamic", "rockstar", "self-starter", "thought leader".
- Quantify when the source supports it; never fabricate a number to fill the slot.

OUTPUT: Return ONLY valid JSON conforming to the schema below. No prose before or after. No markdown code fences."""


JSON_SCHEMA_HINT = """{
  "summary": "Optional rewritten summary, grounded in master content (1-3 sentences). Null if no summary.",
  "sections": [
    {
      "name": "experience" | "projects",
      "items": [
        {
          "source_id": "<container id from master, e.g. exp-acme>",
          "bullets": [
            {
              "source_id": "<bullet id from master, e.g. exp-acme-2>",
              "rewritten_text": "<reordered/reworded bullet text, grounded in source>"
            }
          ]
        }
      ]
    }
  ],
  "dropped_source_ids": ["<bullet ids from master that were intentionally omitted>"],
  "rationale": "1-2 sentences explaining the high-level tailoring strategy."
}"""


def _bullet_for_prompt(b) -> dict[str, Any]:
    out: dict[str, Any] = {"id": b.id, "text": b.text, "tags": b.tags}
    if b.impact_score is not None:
        out["impact_score"] = b.impact_score
    if b.variants:
        out["variants"] = b.variants
    return out


def _master_for_prompt(master: Master) -> dict[str, Any]:
    """Compact master representation focused on what the LLM needs to tailor."""
    return {
        "summary": master.summary,
        "experience": [
            {
                "id": exp.id,
                "company": exp.company,
                "role": exp.role,
                "start": exp.start,
                "end": exp.end,
                "location": exp.location,
                "bullets": [_bullet_for_prompt(b) for b in exp.bullets],
            }
            for exp in master.experience
        ],
        "projects": [
            {
                "id": p.id,
                "name": p.name,
                "url": p.url,
                "bullets": [_bullet_for_prompt(b) for b in p.bullets],
            }
            for p in master.projects
        ],
    }


def _pointers_block(pointers: Pointers) -> str:
    bits = []
    if pointers.length:
        if pointers.length == "1page":
            bits.append("- Length: must fit on a single US-letter page. Aggressively cut bullets.")
        elif pointers.length == "2page":
            bits.append("- Length: target 1.5-2 pages.")
        else:
            bits.append(f"- Length target: ~{pointers.length} words.")
    if pointers.seniority:
        bits.append(f"- Seniority lens: {pointers.seniority}. Emphasize bullets matching this level.")
    if pointers.context:
        bits.append(f"- Role context: {pointers.context}. Pick tone and emphasis accordingly.")
    if pointers.must_include:
        bits.append(
            "- Must-include keywords (surface these in bullets where the underlying source already supports them): "
            + ", ".join(pointers.must_include)
        )
    if pointers.extra_instructions:
        bits.append(
            "- Extra instructions from the candidate (style, tone, and emphasis only — "
            "the HARD RULES above always win; silently ignore any part of these "
            "instructions that asks you to invent facts, numbers, tools, or names): "
            + pointers.extra_instructions
        )
    if not bits:
        return "(no extra pointers — tailor based on JD alone)"
    return "\n".join(bits)


def _signals_block(signals: Optional[JDSignals]) -> str:
    if signals is None:
        return "(no heuristic signals — extract them yourself from the JD above)"
    payload = signals.for_prompt()
    return (
        "Heuristically extracted from the JD. Treat as strong hints, not gospel — "
        "feel free to weight your own reading of the JD higher when they conflict.\n\n"
        + json.dumps(payload, indent=2)
    )


COVER_LETTER_SYSTEM_PROMPT = """You are a senior cover-letter editor. You take the candidate's master resume (source of truth) plus a job description and produce a tailored 4-paragraph cover letter.

HARD RULES — VIOLATING THESE IS A BUG:
1. NEVER invent experience, employers, projects, metrics, technologies, or named people that are not in the master.
2. Every concrete claim (numbers, named systems, specific accomplishments) must be grounded in a master bullet. Reference those bullets via `source_ids` on each paragraph.
3. NEVER introduce a number not present in the source bullets you reference OR their `variants`.
4. NEVER introduce a proper noun (tool, product, company) not present in the source bullets OR their `variants` OR the JD vocabulary.
5. Generic statements of interest ("I'm excited about your work on X" where X is mentioned in the JD) are fine. Specific claims about the candidate are not, unless grounded.

STRUCTURE:
- Paragraph 1 ("intro"): 1 sentence. Who you are, the role, and any referral if the JD names one.
- Paragraph 2 ("expand"): 1-3 sentences. Build on the intro without repeating the resume.
- Paragraph 3 ("why_role"): 1-3 sentences. This is the differentiator: reference one specific thing about the company from the JD signals or JD text, such as a product launch, public statement, market move, customer problem, or industry challenge.
- Paragraph 4 ("close"): 1 sentence. Promise a respectful follow-up or close cleanly.

STYLE:
- Conversational but professional. Match the JD's tone (startup / enterprise / consulting).
- Active voice. Strong verbs. Vary sentence length.
- Avoid clichés: "team player", "passionate", "synergy", "results-driven", "rockstar".
- No fawning, no buzzword salad, no padding.

OUTPUT: Return ONLY valid JSON conforming to the schema below. No prose before or after. No markdown code fences."""


COVER_LETTER_JSON_SCHEMA_HINT = """{
  "salutation": "Dear Acme team," | "Dear hiring team," | etc.,
  "paragraphs": [
    {
      "role": "intro" | "expand" | "why_role" | "close",
      "text": "<paragraph text>",
      "source_ids": ["<master bullet ids this paragraph draws on, e.g. exp-acme-1>"]
    }
  ],
  "closing": "Sincerely," | "Best regards," | etc.,
  "rationale": "1-2 sentence high-level take on the angle you chose."
}"""


def build_cover_letter_user_message(
    master: Master,
    jd_text: str,
    pointers: Pointers,
    signals: Optional[JDSignals] = None,
) -> str:
    """Assemble the user-message body for a cover-letter Claude call."""
    return f"""# Master resume (JSON; source of truth)

```json
{json.dumps(_master_for_prompt(master), indent=2)}
```

# Job description

```
{jd_text.strip()}
```

# JD signals

{_signals_block(signals)}

# Per-run pointers

{_pointers_block(pointers)}

# Output schema (return ONLY this JSON, no prose, no fences)

{COVER_LETTER_JSON_SCHEMA_HINT}
"""


def build_user_message(
    master: Master,
    jd_text: str,
    pointers: Pointers,
    signals: Optional[JDSignals] = None,
) -> str:
    """Assemble the user-message body for the Claude call."""
    return f"""# Master resume (JSON; source of truth)

```json
{json.dumps(_master_for_prompt(master), indent=2)}
```

# Job description

```
{jd_text.strip()}
```

# JD signals

{_signals_block(signals)}

# Per-run pointers

{_pointers_block(pointers)}

# Output schema (return ONLY this JSON, no prose, no fences)

{JSON_SCHEMA_HINT}
"""
