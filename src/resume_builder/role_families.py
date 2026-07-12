"""Role families — the curated list the wizard offers as a first-screen choice.

Sized for the Indian subcontinent job market, both tech and non-tech, with
explicit space for emerging roles via the ``other`` escape hatch. The choice
shapes downstream behaviour: reflection prompts (see :mod:`wizard_prompts`),
tier-rule emphasis (Phase 5), and ATS keyword tables (Phase 7).

Keep this list curated, not exhaustive. ``other`` accepts free-text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class RoleFamily:
    id: str
    label: str
    blurb: str  # one-line description shown next to the option


ROLE_FAMILIES: tuple[RoleFamily, ...] = (
    RoleFamily(
        id="software-engineering",
        label="Software Engineering",
        blurb="Developer, SRE, DevOps, platform, mobile, embedded.",
    ),
    RoleFamily(
        id="data-and-ai",
        label="Data & AI",
        blurb="Data analyst, scientist, ML / AI engineer, MLOps, analytics engineer.",
    ),
    RoleFamily(
        id="product-management",
        label="Product Management",
        blurb="PM, growth PM, product operations, technical PM.",
    ),
    RoleFamily(
        id="design",
        label="Design",
        blurb="UX, UI, product, service, visual, design ops.",
    ),
    RoleFamily(
        id="sales-business-dev",
        label="Sales & Business Development",
        blurb="Sales, BD, partnerships, account management, solutions engineering.",
    ),
    RoleFamily(
        id="marketing",
        label="Marketing",
        blurb="Performance, brand, content, growth, product marketing.",
    ),
    RoleFamily(
        id="consulting-strategy",
        label="Consulting & Strategy",
        blurb="Management consulting, internal strategy, BizOps.",
    ),
    RoleFamily(
        id="finance-accounting",
        label="Finance & Accounting",
        blurb="CA, CFA, FP&A, audit, tax, investment banking, treasury.",
    ),
    RoleFamily(
        id="operations-supply-chain",
        label="Operations & Supply Chain",
        blurb="Operations, logistics, procurement, vendor management, manufacturing.",
    ),
    RoleFamily(
        id="hr-people",
        label="HR & People",
        blurb="HRBP, talent acquisition, L&D, compensation, people operations.",
    ),
    RoleFamily(
        id="academia-research",
        label="Academia & Research",
        blurb="Academic, research scientist, R&D, applied research.",
    ),
    RoleFamily(
        id="healthcare-clinical",
        label="Healthcare & Clinical",
        blurb="Doctor, nurse, allied health, public health, healthcare ops.",
    ),
    RoleFamily(
        id="legal",
        label="Legal",
        blurb="Lawyer, paralegal, compliance, company secretary, contracts.",
    ),
    RoleFamily(
        id="education-teaching",
        label="Education & Teaching",
        blurb="Teacher, professor, EdTech instructor, instructional design.",
    ),
    RoleFamily(
        id="civil-services-government",
        label="Civil Services & Government",
        blurb="IAS / IPS / IFS, PSU, bank PO, government transitioning to corporate.",
    ),
    RoleFamily(
        id="creative-media",
        label="Creative & Media",
        blurb="Journalism, content creation, photography, video, production.",
    ),
    RoleFamily(
        id="non-profit-social",
        label="Non-profit & Social Impact",
        blurb="NGO, development sector, CSR, social entrepreneurship.",
    ),
    RoleFamily(
        id="devrel-community",
        label="DevRel & Community",
        blurb="Developer advocate, community manager, open source, technical writing.",
    ),
    RoleFamily(
        id="other",
        label="Other",
        blurb="Anything else — emerging role, hybrid, freelance. Describe in your own words.",
    ),
)


_BY_ID: dict[str, RoleFamily] = {rf.id: rf for rf in ROLE_FAMILIES}


def all_families() -> List[RoleFamily]:
    return list(ROLE_FAMILIES)


def by_id(role_id: str) -> Optional[RoleFamily]:
    return _BY_ID.get(role_id)


def is_known(role_id: Optional[str]) -> bool:
    return role_id is not None and role_id in _BY_ID
