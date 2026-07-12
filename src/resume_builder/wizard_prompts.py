"""Reflection prompts shown to the user during the brain-dump phase.

The prompts ghost-render as placeholder text inside each chunk's free-text
area. Goal: trigger memory + force specificity, never lead the answer.

Two layers:

- ``BASE_PROMPTS`` — universal questions that apply to every career, every
  role. Drawn from Bock's 8-part series and general reflection practice.
- ``ROLE_PROMPTS`` — role-family-specific additions. Keyed by the IDs in
  :mod:`role_families`. ``other`` falls back to base only.

If a role family has no entry in ``ROLE_PROMPTS``, the user gets the base set.
"""

from __future__ import annotations

from typing import List, Optional


BASE_PROMPTS: tuple[str, ...] = (
    "What's the one thing you shipped, owned, decided, or changed in this period?",
    "What did your manager (or a mentor / client) call out about your work?",
    "What still exists today because of something you did?",
    "Who did you mentor, unblock, hire, or visibly help?",
    "What broke, and what did you fix? Even the small recoveries count.",
    "What did you say no to — and what did that cost or save?",
    "What moved a number — what was the before / after?",
    "Side projects, talks, writing, mentorship, volunteer work — anything outside the day job?",
    "What were you *known for* during this stretch?",
    "What did you learn the hard way that changed how you work now?",
)


# Role-specific prompts piggyback on the base set. Keep them short, pointed,
# and *opening questions*, not instructions. Each surfaces a kind of evidence
# that recruiters in that field actually look for.
ROLE_PROMPTS: dict[str, tuple[str, ...]] = {
    "software-engineering": (
        "What service, feature, or system did you ship, rewrite, or retire?",
        "What latency, throughput, reliability, or cost number did you move?",
        "What technical debt did you eliminate? What incident did you root-cause?",
        "What review or design call changed how a teammate built something?",
        "What did you automate that used to be manual?",
    ),
    "data-and-ai": (
        "What dataset, pipeline, model, or dashboard did you ship?",
        "What decision changed because of analysis you produced?",
        "What accuracy / latency / cost number did your model move?",
        "What manual reporting did you eliminate? How many hours saved per week?",
        "What experiment did you run — what did you learn vs. what you expected?",
    ),
    "product-management": (
        "What feature shipped under your ownership? What metric did it move?",
        "What did you kill or de-prioritise, and what did that unlock?",
        "What research, data, or customer insight changed the roadmap?",
        "What cross-functional fire did you fight to keep the team unblocked?",
        "What strategy doc, PRD, or memo travelled further than its first reader?",
    ),
    "design": (
        "What did you design that's now in production? Link a screen or flow.",
        "What user pain did you measure before and after the change?",
        "What design system, token library, or pattern did you contribute?",
        "What research finding flipped a product decision?",
        "What handoff or critique practice did you improve for the team?",
    ),
    "sales-business-dev": (
        "What deals did you close — sizes, named accounts, sales cycle length?",
        "What pipeline did you build or hand off, and at what conversion?",
        "What process, playbook, or pitch did you create that others now reuse?",
        "What partnership, channel, or geography did you open?",
        "What quota / target did you hit or beat — by how much?",
    ),
    "marketing": (
        "What campaign did you ship? What was the spend, reach, and result?",
        "What channel did you start, scale, or shut down?",
        "What organic or paid metric did you move — by how much, vs. what baseline?",
        "What content, brand asset, or system did you build that the team still uses?",
        "What launch did you own end-to-end?",
    ),
    "consulting-strategy": (
        "What client / business problem did you solve? What was the outcome?",
        "What recommendation did the client / leadership actually act on?",
        "What analysis, model, or framework did you build that travelled past one project?",
        "What team did you lead or workstream did you own — size and scope?",
        "What unprompted insight did you bring that nobody else saw?",
    ),
    "finance-accounting": (
        "What audit, filing, close, or financial review did you lead?",
        "What process did you tighten? What error rate or close-time did you reduce?",
        "What savings, recovery, or revenue did you identify or unlock?",
        "What compliance, tax, or regulatory matter did you resolve?",
        "What model, forecast, or analysis did you build that informed a real decision?",
    ),
    "operations-supply-chain": (
        "What process, SLA, or throughput number did you move?",
        "What cost did you take out — vendor, freight, inventory, headcount?",
        "What system, SOP, or playbook did you create or overhaul?",
        "What crisis (stock-out, shutdown, recall) did you manage through?",
        "What scale did you operate at — orders / day, sites, SKUs, headcount?",
    ),
    "hr-people": (
        "What hires did you close — roles, levels, time-to-fill?",
        "What programme (L&D, performance, comp, DEI) did you design or run?",
        "What retention, engagement, or attrition number did you move?",
        "What policy did you write or revise? Who was affected, how was it received?",
        "What hard conversation, exit, or restructure did you handle well?",
    ),
    "academia-research": (
        "What did you publish, present, or submit — venue and reception?",
        "What grant, funding, or fellowship did you secure?",
        "What experiment, study, or project did you lead?",
        "Who did you supervise, advise, or co-author with?",
        "What instrument, dataset, code, or method did you build that others now use?",
    ),
    "healthcare-clinical": (
        "What patient volume / case mix did you handle?",
        "What clinical outcome, quality metric, or protocol did you improve?",
        "What procedure, surgery, or treatment did you perform — counts, complexity?",
        "What training, rotation, or fellowship did you complete?",
        "What teaching, research, or community-health work did you do alongside?",
    ),
    "legal": (
        "What matter, deal, or case did you handle? Size, parties, outcome?",
        "What contract / playbook / template did you create or standardise?",
        "What regulatory or compliance risk did you identify or close?",
        "What court appearance, deposition, or negotiation did you lead?",
        "What internal training or knowledge resource did you build?",
    ),
    "education-teaching": (
        "What did you teach — subject, level, class size?",
        "What learning outcome or assessment number did you move?",
        "What curriculum, lesson plan, or programme did you design?",
        "Who did you mentor — students, junior teachers — and where did they go?",
        "What award, recognition, or feedback signal did you receive?",
    ),
    "civil-services-government": (
        "What programme, scheme, or initiative did you implement or oversee?",
        "What population, district, or department did you serve — scale?",
        "What measurable change did you drive — coverage, delivery, savings?",
        "What inter-agency or stakeholder coordination did you lead?",
        "What recognition, transfer, or assignment signalled trust?",
    ),
    "creative-media": (
        "What piece, story, campaign, or production did you make?",
        "What audience size, distribution, or pickup did it reach?",
        "What client, publication, or platform did you work with?",
        "What award, festival, or recognition did you receive?",
        "What craft did you visibly grow in this period?",
    ),
    "non-profit-social": (
        "What programme, intervention, or campaign did you run?",
        "What beneficiary scale and outcome did you achieve?",
        "What funding did you raise or grant did you win?",
        "What partnership, coalition, or community did you build?",
        "What field learning changed how the org operates?",
    ),
    "devrel-community": (
        "What content did you ship — talks, posts, videos, docs — and what reach?",
        "What community did you grow — members, contributors, events?",
        "What developer pain did you reduce — feedback into product?",
        "What open-source contribution or sample did you ship?",
        "What partnership or speaking circuit did you open up?",
    ),
}


def reflection_prompts(role_family: Optional[str]) -> List[str]:
    """Return the prompts to surface for a chunk.

    Combines base prompts with role-family-specific ones. Unknown or missing
    role family falls back to base only.
    """
    base = list(BASE_PROMPTS)
    if role_family and role_family in ROLE_PROMPTS:
        return base + list(ROLE_PROMPTS[role_family])
    return base
