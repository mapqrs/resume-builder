"""Style lints applied AFTER the no-invention guard. Advisory, not blocking.

The guard is about truth (don't invent things). Lints are about taste:
- anti-cliché: weasel words and vapid phrases
- verb-diversity: same opening verb repeated across many bullets
- impact-density: bullets without numbers when nearly every sibling has them
- length-target: total word count vs. the requested length pointer

A LintWarning is informational. The CLI prints them; the web UI shows them
in the same panel as guard warnings (with a `kind: "lint"` tag).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import List, Optional

from .bock_tier import classify_bullet
from .schema import Master, Pointers, TailoredBullet, TailoredResume


# Curated list of resume clichés. Match case-insensitively as whole words/phrases.
# Each entry pairs the cliché with a one-line "do this instead" hint.
_CLICHES: list[tuple[str, str]] = [
    ("team player",        "name a specific team outcome you owned"),
    ("results-driven",     "show the result with a number"),
    ("results driven",     "show the result with a number"),
    ("results oriented",   "show the result with a number"),
    ("results-oriented",   "show the result with a number"),
    ("self-starter",       "describe a project you initiated unprompted"),
    ("self starter",       "describe a project you initiated unprompted"),
    ("go-getter",          "describe a project you initiated unprompted"),
    ("go getter",          "describe a project you initiated unprompted"),
    ("rockstar",           "describe what made the work strong"),
    ("rock star",          "describe what made the work strong"),
    ("ninja",              "describe what made the work strong"),
    ("guru",               "describe what made the work strong"),
    ("thought leader",     "name the talk, post, or decision you actually led"),
    ("synergy",            "say what teams collaborated and on what"),
    ("synergies",          "say what teams collaborated and on what"),
    ("dynamic",            "name the specific quality you mean"),
    ("passionate",         "show the passion via a project, not the word"),
    ("hardworking",        "describe the workload concretely"),
    ("hard-working",       "describe the workload concretely"),
    ("hit the ground running", "name the first concrete win"),
    ("best in class",      "show the metric"),
    ("best-in-class",      "show the metric"),
    ("world-class",        "show the metric"),
    ("world class",        "show the metric"),
    ("cutting edge",       "name the technology and why it mattered"),
    ("cutting-edge",       "name the technology and why it mattered"),
    ("bleeding edge",      "name the technology and why it mattered"),
    ("seasoned",           "say how many years"),
    ("proven track record", "show the record with numbers"),
    ("strong communicator", "name a specific written or spoken artifact"),
    ("excellent communication skills", "name a specific written or spoken artifact"),
    ("detail-oriented",    "describe a quality bar you held"),
    ("detail oriented",    "describe a quality bar you held"),
    ("out of the box",     "name the unconventional choice you made"),
    ("out-of-the-box",     "name the unconventional choice you made"),
    ("think outside the box", "name the unconventional choice you made"),
    ("wear many hats",     "list the actual roles you filled"),
    ("multitask",          "name the parallel projects"),
    ("guru",               "describe what made the work strong"),
    ("leverage",           "use 'use' or 'apply' — 'leverage' is jargon"),
    ("leveraged",          "use 'used' or 'applied'"),
    ("leveraging",         "use 'using' or 'applying'"),
    ("game-changer",       "describe what changed and why"),
    ("game changer",       "describe what changed and why"),
    ("paradigm shift",     "describe what changed and why"),
    ("synergized",         "say what teams collaborated"),
    ("ownership mindset",  "describe what you owned"),
]


# Verb-diversity threshold: warn when one opening verb covers > THIS share of
# all tailored bullets. Tuned to be quiet on small resumes (3-4 bullets).
_VERB_REPEAT_THRESHOLD = 3       # absolute count
_VERB_REPEAT_SHARE = 0.40        # OR > this fraction of total bullets


@dataclass
class LintWarning:
    rule: str  # short stable id: "cliche", "verb-diversity", "impact-density", "length"
    message: str
    source_id: Optional[str] = None
    snippet: Optional[str] = None
    suggestion: Optional[str] = None


def _all_tailored_bullets(t: TailoredResume) -> List[TailoredBullet]:
    out: List[TailoredBullet] = []
    for s in t.sections:
        for it in s.items:
            out.extend(it.bullets)
    return out


def _opening_verb(text: str) -> Optional[str]:
    """Return the first word, lowercased, stripped of punctuation. None if empty."""
    m = re.match(r"\s*([A-Za-z][A-Za-z\-]*)", text)
    if not m:
        return None
    return m.group(1).lower()


def _word_count(text: str) -> int:
    return len([w for w in re.split(r"\s+", text.strip()) if w])


def lint_cliches(tailored: TailoredResume) -> List[LintWarning]:
    """Flag bullets / summary lines containing resume clichés."""
    out: List[LintWarning] = []
    haystacks: list[tuple[Optional[str], str]] = []
    if tailored.summary:
        haystacks.append(("<summary>", tailored.summary))
    for b in _all_tailored_bullets(tailored):
        haystacks.append((b.source_id, b.rewritten_text))

    for sid, text in haystacks:
        lower = text.lower()
        for phrase, suggestion in _CLICHES:
            # Whole-word/phrase match, allowing flexible word boundary
            pattern = r"\b" + re.escape(phrase) + r"\b"
            if re.search(pattern, lower):
                out.append(
                    LintWarning(
                        rule="cliche",
                        message=f"cliché '{phrase}' — {suggestion}",
                        source_id=sid,
                        snippet=text[:200],
                        suggestion=suggestion,
                    )
                )
                # Only one cliché-warning per bullet to avoid spam
                break
    return out


def lint_verb_diversity(tailored: TailoredResume) -> List[LintWarning]:
    """Flag opening verbs that repeat across too many bullets."""
    bullets = _all_tailored_bullets(tailored)
    if len(bullets) < 3:
        return []
    verbs = [_opening_verb(b.rewritten_text) for b in bullets]
    counts = Counter(v for v in verbs if v)
    total = len(bullets)
    out: List[LintWarning] = []
    for verb, n in counts.most_common():
        if n >= _VERB_REPEAT_THRESHOLD or n / total > _VERB_REPEAT_SHARE:
            if n < 2:
                continue
            offenders = [
                b.source_id
                for b in bullets
                if _opening_verb(b.rewritten_text) == verb
            ]
            out.append(
                LintWarning(
                    rule="verb-diversity",
                    message=(
                        f"opening verb '{verb}' used {n}× across {total} bullets "
                        f"({offenders[:5]}{'...' if len(offenders) > 5 else ''}). "
                        "Vary verbs: built, shipped, instrumented, productionized, drove, owned, scaled."
                    ),
                    source_id=None,
                    snippet=None,
                    suggestion="vary opening verbs across bullets",
                )
            )
    return out


def lint_impact_density(tailored: TailoredResume) -> List[LintWarning]:
    """If 60%+ of bullets have numeric impact and a given bullet has none, warn.
    Only fires when there's enough sample size (>= 5 bullets).
    """
    bullets = _all_tailored_bullets(tailored)
    if len(bullets) < 5:
        return []
    has_number = [bool(re.search(r"\d", b.rewritten_text)) for b in bullets]
    share = sum(has_number) / len(bullets)
    if share < 0.60:
        return []
    out: List[LintWarning] = []
    for b, has in zip(bullets, has_number):
        if not has:
            out.append(
                LintWarning(
                    rule="impact-density",
                    message=(
                        f"bullet has no quantifiable impact while "
                        f"{int(share*100)}% of siblings do — consider surfacing a number "
                        "if the source bullet supports one."
                    ),
                    source_id=b.source_id,
                    snippet=b.rewritten_text[:200],
                    suggestion="add a number that's already in the source",
                )
            )
    return out


def lint_bock_tier(tailored: TailoredResume) -> List[LintWarning]:
    """Flag bullets that fall short of Bock's XYZ "awesome" form."""
    out: List[LintWarning] = []
    for b in _all_tailored_bullets(tailored):
        tier, missing = classify_bullet(b.rewritten_text)
        if tier == "awesome":
            continue
        if tier == "original":
            if "x_strong_verb" in missing:
                message = (
                    "Bock tier: original. Open with a stronger accomplishment verb, "
                    "then add a measured result."
                )
                suggestion = "replace responsibility wording with an accomplishment"
            else:
                message = (
                    "Bock tier: original. Missing a number. What was measured "
                    "(count, %, $, time)?"
                )
                suggestion = "add a metric already supported by the source"
        else:
            message = (
                "Bock tier: better. Add a 'by [doing Z]' clause naming the method."
            )
            suggestion = "add the method: by doing what, using what, or through what?"
        out.append(
            LintWarning(
                rule="bock-tier",
                message=message,
                source_id=b.source_id,
                snippet=b.rewritten_text[:200],
                suggestion=suggestion,
            )
        )
    return out


def lint_bock_brand_drop(
    tailored: TailoredResume, master: Optional[Master] = None
) -> List[LintWarning]:
    """Suggest including recognizable company/project names already in master tags."""
    if master is None:
        return []
    known_names: dict[str, str] = {}
    for exp in master.experience:
        known_names[exp.company.strip().lower()] = exp.company.strip()
    for proj in master.projects:
        known_names[proj.name.strip().lower()] = proj.name.strip()

    out: List[LintWarning] = []
    for b in _all_tailored_bullets(tailored):
        src = master.bullet_by_id(b.source_id)
        if src is None:
            continue
        text_lower = b.rewritten_text.lower()
        for tag in src.tags:
            tag_lower = tag.strip().lower()
            name = known_names.get(tag_lower)
            if not name or name.lower() in text_lower:
                continue
            out.append(
                LintWarning(
                    rule="bock-brand-drop",
                    message=(
                        f"Bock brand-name signal: source tags include {name}, "
                        "but the tailored bullet omits it."
                    ),
                    source_id=b.source_id,
                    snippet=b.rewritten_text[:200],
                    suggestion=f"mention {name} if it fits naturally and is truthful",
                )
            )
            break
    return out


def lint_length_target(
    tailored: TailoredResume, pointers: Optional[Pointers] = None
) -> List[LintWarning]:
    """Compare total word count to the pointer's length target."""
    if not pointers or not pointers.length:
        return []
    total = _word_count(tailored.summary or "")
    for b in _all_tailored_bullets(tailored):
        total += _word_count(b.rewritten_text)
    target_min: Optional[int] = None
    target_max: Optional[int] = None
    if pointers.length == "1page":
        target_min, target_max = 350, 550
    elif pointers.length == "2page":
        target_min, target_max = 700, 1000
    else:
        try:
            n = int(pointers.length)
            target_min, target_max = int(n * 0.85), int(n * 1.15)
        except (TypeError, ValueError):
            return []

    if target_max is not None and total > target_max:
        return [
            LintWarning(
                rule="length",
                message=(
                    f"output is ~{total} words; target {pointers.length} "
                    f"is {target_min}-{target_max}. Tailor again with stricter cuts, "
                    "or drop the lowest-impact bullets."
                ),
                suggestion="cut bullets with lower impact_score",
            )
        ]
    if target_min is not None and total < target_min:
        return [
            LintWarning(
                rule="length",
                message=(
                    f"output is ~{total} words; target {pointers.length} "
                    f"is {target_min}-{target_max}. The page will look sparse — "
                    "surface more bullets or expand summary."
                ),
                suggestion="surface more high-impact bullets from master",
            )
        ]
    return []


def lint(
    tailored: TailoredResume,
    pointers: Optional[Pointers] = None,
    master: Optional[Master] = None,
) -> List[LintWarning]:
    """Run all lints. Returns a single combined list, ordered for readability:
    length first (strategic), verb-diversity (cross-bullet), then per-bullet rules.
    """
    out: List[LintWarning] = []
    out.extend(lint_length_target(tailored, pointers))
    out.extend(lint_verb_diversity(tailored))
    out.extend(lint_cliches(tailored))
    out.extend(lint_bock_tier(tailored))
    out.extend(lint_bock_brand_drop(tailored, master))
    out.extend(lint_impact_density(tailored))
    out.extend(lint_typos(tailored, master))
    return out


# ---------- Phase 5: spell-check (Bock Part 02 — 58% of resumes have typos) ----------


# Tokens we never flag regardless of dictionary: punctuation-only,
# numbers, acronyms (all-caps 2-5 chars), tech symbols.
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z\-']{2,}")


def _master_allowlist(master: Optional[Master]) -> set[str]:
    """Build the set of proper-noun-like tokens that should never be flagged.

    Pulls company names, role titles, project names, school names, every
    skill item, and every award name + criteria. The set is lowercased
    because the spell-checker's dictionary itself is lowercased.
    """
    out: set[str] = set()
    if master is None:
        return out

    def _add(text: Optional[str]) -> None:
        if not text:
            return
        for m in _TOKEN_RE.finditer(text):
            out.add(m.group(0).lower())

    for exp in master.experience:
        _add(exp.company)
        _add(exp.role)
        _add(exp.location)
    for proj in master.projects:
        _add(proj.name)
    for edu in master.education:
        _add(edu.school)
        _add(edu.degree)
        _add(edu.location)
        for award in edu.awards:
            _add(award.name)
            _add(award.criteria)
    for grp in master.skills:
        _add(grp.category)
        for item in grp.items:
            _add(item)
    return out


def _try_spellchecker():
    """Lazy import — keeps `pyspellchecker` as an optional dependency.

    Returns ``None`` if the package isn't installed; callers degrade
    gracefully to an empty warning list.
    """
    try:
        from spellchecker import SpellChecker  # type: ignore
    except ImportError:
        return None
    return SpellChecker(language="en", distance=1)


def lint_typos(
    tailored: TailoredResume,
    master: Optional[Master] = None,
) -> List[LintWarning]:
    """Flag suspect spellings across every rewritten bullet + summary.

    Bock Part 02: "58% of resumes have typos. Recruiters use them as a
    fast filter to reject candidates." This lint is the line of defense
    after the no-invention guard.

    Rules:
    - Compare each alphabetic token against a standard English dictionary.
    - Allowlist tokens that appear in the master metadata (company / role /
      project / school / skill / award names) so India-specific or
      domain-specific proper nouns don't false-positive.
    - Skip pure-digit / mixed alphanumeric / all-caps acronym tokens —
      they're not English-dictionary candidates.
    - Skip tokens shorter than 3 characters.
    """
    spell = _try_spellchecker()
    if spell is None:
        return []

    allowlist = _master_allowlist(master)
    seen_per_bullet: dict[tuple[str, str], set[str]] = {}
    out: List[LintWarning] = []

    candidates_by_bullet: list[tuple[Optional[str], str, set[str]]] = []
    for b in _all_tailored_bullets(tailored):
        tokens = _candidate_tokens(b.rewritten_text)
        if tokens:
            candidates_by_bullet.append((b.source_id, b.rewritten_text, tokens))
    if tailored.summary:
        tokens = _candidate_tokens(tailored.summary)
        if tokens:
            candidates_by_bullet.append((None, tailored.summary, tokens))

    for source_id, text, tokens in candidates_by_bullet:
        flagged = [
            t for t in tokens
            if t.lower() not in allowlist and t.lower() in spell.unknown([t.lower()])
        ]
        if not flagged:
            continue
        # Suggest the first correction the library offers per typo.
        for token in flagged:
            correction = spell.correction(token.lower())
            suggestion = (
                f"did you mean '{correction}'?"
                if correction and correction != token.lower()
                else "check spelling"
            )
            out.append(
                LintWarning(
                    rule="typo-suspect",
                    message=f"'{token}' looks like a typo",
                    source_id=source_id,
                    snippet=text,
                    suggestion=suggestion,
                )
            )
    return out


def _candidate_tokens(text: str) -> set[str]:
    """Tokens that are eligible for spell-checking.

    Excludes:
    - Tokens shorter than 3 chars
    - All-caps tokens that look like acronyms (length 2-5, all upper)
    - Tokens with any non-letter character (numbers, hyphens, etc. handled
      via the regex match itself — only letters / hyphens / apostrophes)
    """
    out: set[str] = set()
    for m in _TOKEN_RE.finditer(text):
        token = m.group(0)
        if len(token) < 3:
            continue
        if token.isupper() and 2 <= len(token) <= 5:
            continue  # acronym
        # The regex permits hyphens / apostrophes; spell-check expects plain words.
        if "-" in token or "'" in token:
            continue
        out.add(token)
    return out
