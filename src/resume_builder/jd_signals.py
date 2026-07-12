"""Heuristic JD signal extractor.

Deterministic, regex-driven, runs in <1ms on a typical JD. Produces structured
signals — must-haves, top tech keywords, inferred seniority, role archetype,
scope phrases, soft skills — that get fed to the tailor as explicit context.

The LLM still sees the raw JD too, so it can refine; the heuristic just makes
sure the strong signals don't get lost in noise.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import List, Optional

from pydantic import BaseModel, Field

# Imported lazily inside the function body to avoid a circular import — schema
# imports nothing from jd_signals, but the type is used only by the JD-less
# path. The TYPE_CHECKING guard documents intent for type checkers.
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .schema import TargetRole


# ---------- patterns ----------

_BULLET_RE = re.compile(r"^\s*[-•*]\s+(.+?)\s*$")
_NUMBERED_RE = re.compile(r"^\s*\d+[.)]\s+(.+?)\s*$")

# Section header keywords — keys are output bucket names, values are header phrases
_SECTION_HEADERS: dict[str, list[str]] = {
    "must_have": [
        "requirements", "qualifications", "what you need", "what you'll bring",
        "what you bring", "minimum qualifications", "must have", "must-have",
        "required", "you have", "we're looking for", "we are looking for",
        "basic qualifications",
    ],
    "nice_to_have": [
        "nice to have", "nice-to-have", "bonus", "preferred qualifications",
        "preferred", "pluses", "extra credit", "you might also have",
        "bonus points",
    ],
    "responsibilities": [
        "responsibilities", "what you'll do", "what you will do",
        "the role", "your role", "day to day", "day-to-day",
        "what you'll be doing",
    ],
    "about": [
        "about us", "the company", "who we are", "our mission",
    ],
}

_SENIORITY_TITLE_KEYWORDS: dict[str, str] = {
    "principal":          "principal",
    "staff":              "staff",
    "senior staff":       "staff",
    "founding":           "founding-eng",
    "founding engineer":  "founding-eng",
    "senior":             "senior",
    "sr.":                "senior",
    "lead":               "lead",
    "tech lead":          "lead",
    "manager":            "manager",
    "engineering manager": "manager",
    "director":           "manager",
    "junior":             "ic",
    "associate":          "ic",
    "entry":              "ic",
    "intern":             "ic",
}

_ARCHETYPE_KEYWORDS: dict[str, list[str]] = {
    "backend": [
        "backend", "back-end", "back end", "api ", "apis ", "server-side",
        "distributed systems", "microservices", "rest api", "graphql",
    ],
    "frontend": [
        "frontend", "front-end", "front end", "react", "vue", "angular",
        "next.js", "css", "ui engineer", "client-side",
    ],
    "fullstack": [
        "full stack", "full-stack", "fullstack", "end-to-end product",
    ],
    "infra": [
        "infrastructure", "platform engineer", "devops", "sre",
        "site reliability", "kubernetes", "terraform", "observability",
        "cloud engineer",
    ],
    "ml": [
        "machine learning", "ml engineer", "ai engineer", "deep learning",
        "model training", "inference", "llm", "computer vision", "nlp",
    ],
    "data": [
        "data engineer", "data engineering", "etl", "data warehouse",
        "data pipeline", "spark", "airflow", "dbt",
    ],
    "mobile": [
        "ios engineer", "android engineer", "mobile engineer", "swift",
        "kotlin", "react native",
    ],
    "security": [
        "security engineer", "appsec", "infosec", "application security",
    ],
    "data-science": [
        "data scientist", "analytics engineer", "quantitative",
    ],
}

# Pattern for proper-noun-y tech tokens. Same shape as guard's, intentionally;
# we want the same set of "candidate names" out of the JD.
_PROPER_NOUN_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9]*(?:[A-Z][A-Za-z0-9]*)*|[a-z]+[A-Z][A-Za-z0-9]*|k8s|s3)\b"
)

# Words that look like tech-y proper nouns but aren't.
_TECH_STOPWORDS = {
    # filler
    "We", "You", "The", "Our", "Your", "What", "How", "When", "Where", "Why",
    "About", "And", "Or", "But", "If", "It", "This", "That", "These", "Those",
    # meta
    "Working", "Experience", "Knowledge", "Expertise", "Strong", "Excellent",
    "Great", "Highly", "Deep", "Solid", "Proven", "Demonstrated", "Hands-on",
    # role words
    "Engineer", "Engineering", "Engineers", "Senior", "Staff", "Principal",
    "Junior", "Lead", "Manager", "Director", "VP", "CTO", "CEO", "CFO", "COO",
    "Software", "Developer", "Developers", "Team", "Teams",
    # archetype labels — captured separately as role_archetype; not useful as keywords
    "Backend", "Frontend", "Fullstack", "Platform", "Infrastructure",
    "Mobile", "Security", "Data",
    # section header tokens (leak through when we extract from raw JD text)
    "Requirements", "Responsibilities", "Qualifications", "Bonus",
    "Preferred", "Required", "Nice", "Must",
    # common imperative verbs at start of responsibility bullets — captured
    # as Capitalized but not actually proper nouns
    "Drive", "Lead", "Mentor", "Partner", "Build", "Built", "Design", "Designed",
    "Own", "Owned", "Manage", "Managed", "Develop", "Developed", "Ship", "Shipped",
    "Deliver", "Delivered", "Implement", "Implemented", "Improve", "Improved",
    "Optimize", "Optimized", "Architect", "Architected", "Collaborate", "Communicate",
    "Maintain", "Operate", "Scale", "Support", "Identify", "Open",
    "Background", "Comfort", "Strong", "Solid", "Excellent",
    # time
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
    "Sunday", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
    "Q1", "Q2", "Q3", "Q4",
    # geo
    "USA", "EU", "UK", "US", "EMEA", "APAC", "LATAM",
    # generic biz
    "Inc", "LLC", "Corp", "Co", "Ltd", "Mr", "Ms", "Dr",
}

_YEARS_RE = re.compile(r"(\d+)\s*\+?\s*years?", re.I)

# Soft skills: pattern -> canonical label
_SOFT_SKILLS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bwritten\s+communication\b", re.I),    "written communication"),
    (re.compile(r"\bverbal\s+communication\b", re.I),     "verbal communication"),
    (re.compile(r"\bcommunicat(?:e|ion|ing|or|ors)\b", re.I), "communication"),
    (re.compile(r"\bmentor(?:ship|ing|ed|s)?\b", re.I),   "mentorship"),
    (re.compile(r"\bcoach(?:ing)?\b", re.I),              "mentorship"),
    (re.compile(r"\bcross[- ]functional\w*\b", re.I),     "cross-functional"),
    (re.compile(r"\bstakeholders?\b", re.I),              "stakeholder management"),
    (re.compile(r"\bleadership\b", re.I),                 "leadership"),
    (re.compile(r"\bownership\b", re.I),                  "ownership"),
    (re.compile(r"\bself[- ]direct(?:ed|ion)\b", re.I),   "self-direction"),
    (re.compile(r"\bautonom(?:y|ous)\b", re.I),           "autonomy"),
    (re.compile(r"\b(?:public\s+speaking|presentation\s+skills)\b", re.I), "presentation"),
    (re.compile(r"\bproblem[- ]solv(?:e|er|ing)\b", re.I), "problem solving"),
]

# Scope/scale phrases that indicate the system size.
_SCOPE_PATTERNS: list[re.Pattern] = [
    re.compile(r"\b\d+\s*[KMB]\+?\s*(?:users?|customers?|requests?|events?|transactions?|rows?|messages?|writes?|reads?|nodes?)\b", re.I),
    re.compile(r"\b\d+\s*[KMB]\+?\s*(?:RPS|QPS|TPS)\b"),
    re.compile(r"\bpetabytes?\b", re.I),
    re.compile(r"\bexabytes?\b", re.I),
    re.compile(r"\bterabytes?\b", re.I),
    re.compile(r"\bmillions?\s+of\s+\w+\b", re.I),
    re.compile(r"\bbillions?\s+of\s+\w+\b", re.I),
    re.compile(r"\btrillions?\s+of\s+\w+\b", re.I),
    re.compile(r"\bhigh[- ]traffic\b", re.I),
    re.compile(r"\b(?:multi[- ]region|globally distributed)\b", re.I),
]


# ---------- output schema ----------


class JDSignals(BaseModel):
    """Structured signals heuristically extracted from a job description."""

    title: Optional[str] = None
    inferred_seniority: Optional[str] = None  # ic | senior | staff | principal | lead | manager | founding-eng
    role_archetype: Optional[str] = None      # backend | frontend | fullstack | infra | ml | data | mobile | security | data-science
    must_haves: List[str] = Field(default_factory=list)
    nice_to_haves: List[str] = Field(default_factory=list)
    responsibilities: List[str] = Field(default_factory=list)
    top_keywords: List[str] = Field(default_factory=list)
    scope_signals: List[str] = Field(default_factory=list)
    soft_skills: List[str] = Field(default_factory=list)
    company_specifics: List[str] = Field(default_factory=list)
    years_required: Optional[int] = None      # min years from any "X+ years" phrase

    def for_prompt(self) -> dict:
        """Compact dict for embedding in the LLM user message."""
        return self.model_dump(exclude_none=True, exclude_defaults=False)


# ---------- helpers ----------


def _classify_header(line: str) -> Optional[str]:
    """Return the section bucket name if `line` reads like a section header, else None."""
    stripped = line.strip().rstrip(":").lower()
    if not stripped or len(stripped) > 60:
        return None
    # Must look heading-y: short, often followed by a colon, often title-case
    for bucket, phrases in _SECTION_HEADERS.items():
        for phrase in phrases:
            if stripped == phrase or stripped.startswith(phrase + ":") or stripped == phrase + ":":
                return bucket
            # A header line that's MOSTLY the phrase (e.g. "Required Qualifications")
            if phrase in stripped and len(stripped) <= len(phrase) + 25:
                # sanity-check: header lines rarely end in a period
                if not stripped.endswith("."):
                    return bucket
    return None


def _bullet_text(line: str) -> Optional[str]:
    m = _BULLET_RE.match(line) or _NUMBERED_RE.match(line)
    return m.group(1).strip() if m else None


def _split_into_sections(jd_text: str) -> dict[str, list[str]]:
    """Walk the JD and return {bucket_name: [bullet_lines]}.

    Lines outside any recognized section are kept in 'unscoped'.
    """
    out: dict[str, list[str]] = {
        "must_have": [],
        "nice_to_have": [],
        "responsibilities": [],
        "about": [],
        "unscoped": [],
    }
    current = "unscoped"
    for raw in jd_text.splitlines():
        # Section header?
        bucket = _classify_header(raw)
        if bucket:
            current = bucket
            continue
        # Bullet?
        bullet = _bullet_text(raw)
        if bullet:
            out[current].append(bullet)
    return out


def _extract_title(jd_text: str) -> Optional[str]:
    """Return the first non-empty, short, capitalized line — usually the role title."""
    for raw in jd_text.splitlines():
        s = raw.strip()
        if not s or len(s) > 90:
            continue
        # Skip obvious non-title lines
        if s.endswith((".", "?", "!")):
            continue
        # Title-case-y check: at least 2 words, first letter uppercase
        if not s[0].isupper():
            continue
        if len(s.split()) < 2:
            continue
        return s
    return None


def _infer_seniority(title: Optional[str], full_text: str) -> Optional[str]:
    """Look in the title first, then anywhere else for seniority cues."""
    haystacks = [(title or "").lower(), full_text.lower()[:600]]  # title + intro
    for hay in haystacks:
        if not hay:
            continue
        # Check multi-word keys before single-word (so "senior staff" beats "senior")
        for keyword in sorted(_SENIORITY_TITLE_KEYWORDS, key=len, reverse=True):
            if re.search(r"\b" + re.escape(keyword) + r"\b", hay):
                return _SENIORITY_TITLE_KEYWORDS[keyword]
    return None


def _infer_archetype(title: Optional[str], full_text: str) -> Optional[str]:
    """Score each archetype by keyword hits in title (3x) + body (1x). Highest wins."""
    title_l = (title or "").lower()
    body_l = full_text.lower()
    scores: dict[str, int] = {}
    for arche, keywords in _ARCHETYPE_KEYWORDS.items():
        score = 0
        for kw in keywords:
            if kw in title_l:
                score += 3
            score += body_l.count(kw)
        if score:
            scores[arche] = score
    if not scores:
        return None
    return max(scores.items(), key=lambda kv: kv[1])[0]


def _extract_top_keywords(jd_text: str, n: int = 12) -> list[str]:
    """Pull tech-y proper-noun candidates by frequency. Filter known stopwords."""
    counts: Counter[str] = Counter()
    for m in _PROPER_NOUN_RE.finditer(jd_text):
        token = m.group(1)
        if len(token) < 2:
            continue
        if token in _TECH_STOPWORDS:
            continue
        # Skip pure-digit (shouldn't happen with this regex, defensive)
        if token.isdigit():
            continue
        counts[token] += 1
    # Boost tokens that appear in bullet lines (signal they're skill-listed)
    bullet_text = "\n".join(
        _bullet_text(l) or ""
        for l in jd_text.splitlines()
    )
    for token in list(counts):
        if token in bullet_text:
            counts[token] += 1  # gentle bump
    return [t for t, _ in counts.most_common(n)]


def _extract_years(jd_text: str) -> Optional[int]:
    matches = [int(m.group(1)) for m in _YEARS_RE.finditer(jd_text)]
    if not matches:
        return None
    # Take the smallest "X years" — usually "5+ years" is the floor
    return min(matches)


def _extract_soft_skills(jd_text: str) -> list[str]:
    seen: list[str] = []
    for pat, label in _SOFT_SKILLS:
        if pat.search(jd_text) and label not in seen:
            seen.append(label)
    return seen


def _extract_scope(jd_text: str) -> list[str]:
    out: list[str] = []
    seen_lower: set[str] = set()
    for pat in _SCOPE_PATTERNS:
        for m in pat.finditer(jd_text):
            phrase = m.group(0).strip()
            key = phrase.lower()
            if key in seen_lower:
                continue
            seen_lower.add(key)
            out.append(phrase)
    return out


_COMPANY_SPECIFIC_PATTERNS: list[re.Pattern] = [
    re.compile(r"\b(?:launched|launching|announced|introducing|released|built)\b.*", re.I),
    re.compile(r"\b(?:mission|vision|values?)\b.*", re.I),
    re.compile(r"\b(?:market|industry|customers?|users?)\b.*(?:challenge|problem|opportunity)\b.*", re.I),
    re.compile(r"\b(?:recently|today|now)\b.*", re.I),
]


def _extract_company_specifics(jd_text: str, n: int = 6) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in jd_text.splitlines():
        line = raw.strip().lstrip("-•* ").strip()
        if len(line) < 20 or len(line) > 220:
            continue
        if not any(p.search(line) for p in _COMPANY_SPECIFIC_PATTERNS):
            continue
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
        if len(out) >= n:
            break
    return out


# ---------- public API ----------


def extract(jd_text: str) -> JDSignals:
    """Run all heuristics and return a populated JDSignals."""
    title = _extract_title(jd_text)
    sections = _split_into_sections(jd_text)

    # If the JD has no recognized "Requirements"/"Nice to have" headers, fall
    # back: every bullet in the doc becomes a "must_have" candidate.
    must = sections["must_have"]
    nice = sections["nice_to_have"]
    if not must and not nice:
        must = sections["unscoped"]

    return JDSignals(
        title=title,
        inferred_seniority=_infer_seniority(title, jd_text),
        role_archetype=_infer_archetype(title, jd_text),
        must_haves=must,
        nice_to_haves=nice,
        responsibilities=sections["responsibilities"],
        top_keywords=_extract_top_keywords(jd_text),
        scope_signals=_extract_scope(jd_text),
        soft_skills=_extract_soft_skills(jd_text),
        company_specifics=_extract_company_specifics(jd_text),
        years_required=_extract_years(jd_text),
    )


# ---------- JD-less mode (Phase 7) ----------

# Maps a substring found in the user's role name to a synthesized signal payload.
# Keys are searched case-insensitively against ``target.role``; the first match
# wins. Order matters: put more-specific matches before generic ones.
#
# Why a hand-tuned table instead of LLM lookup? Determinism + cost. The tailor
# itself is the LLM call — this just gives it a richer starting prompt. The
# table is intentionally small and biased toward common Indian + global tech
# roles; users can extend their signal set via ``target.must_include``.
_ROLE_KEYWORD_TABLE: list[tuple[str, dict]] = [
    # Order matters — multi-word matches first.
    ("data engineer", {
        "archetype": "data",
        "keywords": ["data pipelines", "ETL", "Spark", "Airflow", "data warehouse", "SQL"],
        "soft": ["data quality", "lineage", "reliability"],
    }),
    ("data scientist", {
        "archetype": "data-science",
        "keywords": ["statistics", "experiment design", "Python", "modeling", "evaluation"],
        "soft": ["communication", "stakeholder management"],
    }),
    ("machine learning", {
        "archetype": "ml",
        "keywords": ["model training", "evaluation", "inference", "PyTorch", "deployment"],
        "soft": ["experimentation", "scientific rigor"],
    }),
    ("ml engineer", {
        "archetype": "ml",
        "keywords": ["model training", "inference", "deployment", "PyTorch", "MLOps"],
        "soft": ["experimentation"],
    }),
    ("backend", {
        "archetype": "backend",
        "keywords": ["distributed systems", "API design", "performance", "scaling", "databases"],
        "soft": ["ownership", "system design"],
    }),
    ("frontend", {
        "archetype": "frontend",
        "keywords": ["React", "TypeScript", "accessibility", "performance", "design systems"],
        "soft": ["cross-functional", "design collaboration"],
    }),
    ("full stack", {
        "archetype": "fullstack",
        "keywords": ["end-to-end", "React", "TypeScript", "Node.js", "databases"],
        "soft": ["ownership", "shipping speed"],
    }),
    ("fullstack", {
        "archetype": "fullstack",
        "keywords": ["end-to-end", "React", "TypeScript", "Node.js", "databases"],
        "soft": ["ownership", "shipping speed"],
    }),
    ("infra", {
        "archetype": "infra",
        "keywords": ["Kubernetes", "Terraform", "AWS", "observability", "reliability"],
        "soft": ["on-call discipline", "automation"],
    }),
    ("platform", {
        "archetype": "infra",
        "keywords": ["developer platforms", "Kubernetes", "CI/CD", "observability"],
        "soft": ["leverage", "developer experience"],
    }),
    ("devops", {
        "archetype": "infra",
        "keywords": ["CI/CD", "Terraform", "AWS", "Kubernetes", "monitoring"],
        "soft": ["automation", "incident response"],
    }),
    ("sre", {
        "archetype": "infra",
        "keywords": ["reliability", "SLOs", "incident response", "observability", "runbooks"],
        "soft": ["on-call discipline", "post-mortems"],
    }),
    ("mobile", {
        "archetype": "mobile",
        "keywords": ["iOS", "Android", "Swift", "Kotlin", "performance"],
        "soft": ["polish", "ship quality"],
    }),
    ("ios", {
        "archetype": "mobile",
        "keywords": ["Swift", "SwiftUI", "iOS", "performance", "App Store"],
        "soft": ["polish"],
    }),
    ("android", {
        "archetype": "mobile",
        "keywords": ["Kotlin", "Jetpack", "Android", "performance", "Play Store"],
        "soft": ["polish"],
    }),
    ("security", {
        "archetype": "security",
        "keywords": ["threat modeling", "AppSec", "incident response", "auditing"],
        "soft": ["risk assessment", "communication"],
    }),
    # Non-engineering roles — archetype stays None.
    ("product manager", {
        "archetype": None,
        "keywords": ["roadmap", "user research", "metrics", "product strategy", "discovery"],
        "soft": ["stakeholder management", "cross-functional", "written communication"],
    }),
    ("product", {
        "archetype": None,
        "keywords": ["roadmap", "user research", "metrics", "product strategy"],
        "soft": ["stakeholder management", "cross-functional"],
    }),
    ("designer", {
        "archetype": None,
        "keywords": ["user research", "interaction design", "design systems", "prototyping"],
        "soft": ["design collaboration", "communication"],
    }),
    ("design", {
        "archetype": None,
        "keywords": ["user research", "interaction design", "design systems", "prototyping"],
        "soft": ["design collaboration", "communication"],
    }),
    ("data analyst", {
        "archetype": None,
        "keywords": ["SQL", "dashboards", "data quality", "metrics"],
        "soft": ["written communication", "stakeholder management"],
    }),
    ("analyst", {
        "archetype": None,
        "keywords": ["SQL", "Excel", "dashboards", "metrics"],
        "soft": ["written communication", "stakeholder management"],
    }),
    # Generic engineering fallback — runs last.
    ("engineer", {
        "archetype": None,
        "keywords": ["software engineering", "system design", "code quality", "testing"],
        "soft": ["ownership", "cross-functional"],
    }),
]


# Seniority lens → emphasis hints. The tailor consumes these via the
# `soft_skills` slot to bias bullet selection.
_SENIORITY_NOTES: dict[str, list[str]] = {
    "ic": ["execution", "shipping volume"],
    "senior": ["scope", "ownership", "leverage"],
    "staff": ["technical leadership", "cross-team impact", "architecture", "mentorship"],
    "manager": ["team scaling", "1:1s", "hiring", "people development"],
    "founding-eng": ["zero-to-one", "wide ownership", "wearing many hats"],
}


# Company-size lens → scope/scale signals.
_COMPANY_SIZE_NOTES: dict[str, list[str]] = {
    "startup": ["fast iteration", "wide ownership", "scrappy"],
    "scaleup": ["scaling pains", "platformization", "process maturation"],
    "enterprise": ["large-scale systems", "stakeholders", "stability"],
    "faang": ["scale", "system design", "leadership behaviors"],
}


def _match_role_table(role: str) -> dict | None:
    """Find the first table entry whose key is a substring of ``role`` (lc)."""
    role_l = role.lower()
    for key, payload in _ROLE_KEYWORD_TABLE:
        if key in role_l:
            return payload
    return None


def from_target_role(target: "TargetRole") -> JDSignals:
    """Synthesize ``JDSignals`` from a ``TargetRole`` when no JD is on hand.

    The existing tailor + ATS + guard consume the result unchanged — the
    synthesized signals carry must-haves (from ``target.must_include``),
    top keywords (from the role-keyword table + must-include), inferred
    seniority + archetype, and soft signals from the seniority + company-size
    lenses.

    Falls back to a generic engineering payload when the role text doesn't
    match the table at all. Never raises — every TargetRole produces a usable
    JDSignals.
    """
    matched = _match_role_table(target.role) or {}

    archetype = matched.get("archetype")
    base_keywords: List[str] = list(matched.get("keywords", []))
    base_soft: List[str] = list(matched.get("soft", []))

    # Dedup while preserving order: table keywords first, then user must-include.
    seen_kw: set[str] = set()
    top_keywords: List[str] = []
    for kw in base_keywords + list(target.must_include):
        key = kw.lower().strip()
        if not key or key in seen_kw:
            continue
        seen_kw.add(key)
        top_keywords.append(kw.strip())

    # Industry, if present, joins the keyword pool — gives the tailor a hook
    # into the company context without needing a full JD.
    if target.industry:
        key = target.industry.lower().strip()
        if key and key not in seen_kw:
            seen_kw.add(key)
            top_keywords.append(target.industry.strip())

    # Soft skills: table soft signals + seniority lens + company-size lens.
    soft_seen: set[str] = set()
    soft_skills: List[str] = []
    for s in base_soft:
        if s.lower() not in soft_seen:
            soft_seen.add(s.lower())
            soft_skills.append(s)
    if target.seniority:
        for s in _SENIORITY_NOTES.get(target.seniority, []):
            if s.lower() not in soft_seen:
                soft_seen.add(s.lower())
                soft_skills.append(s)

    # Scope: company size hints feed scope_signals so the lens reaches the
    # tailor's selection priorities.
    scope_signals: List[str] = []
    if target.company_size:
        scope_signals.extend(_COMPANY_SIZE_NOTES.get(target.company_size, []))

    return JDSignals(
        title=target.role,
        inferred_seniority=target.seniority,
        role_archetype=archetype,
        # Must-have keywords act as the strongest match target for the tailor;
        # they're what the user explicitly listed as required.
        must_haves=list(target.must_include),
        nice_to_haves=[],
        responsibilities=[],
        top_keywords=top_keywords,
        scope_signals=scope_signals,
        soft_skills=soft_skills,
        company_specifics=[],
        years_required=None,
    )


def target_role_to_jd_text(target: "TargetRole") -> str:
    """Synthesize a JD-shaped text blob from a TargetRole.

    The text isn't shown to the user — it feeds the guard's vocabulary (so
    must-include keywords + industry tokens land in the JD vocab) and the
    LLM's user message (so the tailor sees what's being targeted). Kept
    deliberately structured + short so the tailor doesn't anchor on
    fabricated narrative.
    """
    signals = from_target_role(target)
    lines: List[str] = [target.role]
    if target.seniority:
        lines.append(f"Seniority: {target.seniority}")
    if target.industry:
        lines.append(f"Industry: {target.industry}")
    if target.company_size:
        lines.append(f"Company size: {target.company_size}")
    lines.append("")
    lines.append("This is a synthesized target-role brief (no real JD was provided).")
    lines.append("")
    if signals.top_keywords:
        lines.append("Relevant keywords / technologies:")
        for kw in signals.top_keywords:
            lines.append(f"- {kw}")
        lines.append("")
    if signals.soft_skills:
        lines.append("Emphasis (soft signals):")
        for s in signals.soft_skills:
            lines.append(f"- {s}")
        lines.append("")
    if signals.scope_signals:
        lines.append("Scope:")
        for s in signals.scope_signals:
            lines.append(f"- {s}")
        lines.append("")
    if target.must_include:
        lines.append("Must-include (user-supplied):")
        for k in target.must_include:
            lines.append(f"- {k}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
