#!/usr/bin/env python3
"""
taxonomy.py
-----------
The controlled vocabulary Gate 2 extracts into. Versioned, because a taxonomy
change has to be a data migration rather than a schema one: classify.py reruns
any job_facts row whose taxonomy_version or extraction_model is stale, so
adding a domain tag means a targeted re-extraction, not a backfill of the world.

Bump TAXONOMY_VERSION whenever a value below changes meaning. Adding a new
domain tag is additive and does not require a bump; renaming or resplitting one
does, because stored rows would otherwise mean something different from new ones.
"""

TAXONOMY_VERSION = "pm-board-v1-2026-09"

# Why a role is not on this board. Mirrors filters.py's Gate 1 reasons so the
# two gates speak the same language and the eval can measure them together.
EXCLUSION_REASONS = (
    "program_management",   # TPM / program / project / delivery manager
    "product_marketing",    # PMM
    "solutions_or_fde",     # forward deployed, solutions, presales, customer engineer
    "design",               # product designer, UX
    "engineering",          # SWE, product engineer, product security
    "analytics",            # product/data/business analyst
    "operations",           # product ops
    "leadership",           # director+ -- tagged, not shown
    "other",
)

SENIORITY = (
    "associate",            # APM, junior, PM I
    "mid",                  # PM, PM II, no seniority marker
    "senior",               # senior / sr. PM, PM III
    "staff_principal",      # staff, principal, group, lead PM
    "director_plus",        # director, VP, head of, CPO -- off-board
)

# What the role's day-to-day work touches. Role-level, not company-level --
# a payments PM at an AI company is fintech, not ai_ml.
DOMAIN_TAGS = (
    "ai_ml",                # LLM/agents/applied AI as the product itself
    "data",                 # data platforms, warehousing, analytics products
    "infrastructure",       # cloud, compute, storage, networking
    "developer_tools",      # APIs, SDKs, DX, internal platform
    "security",             # secops, identity, compliance products
    "fintech",              # payments, banking, lending, insurance
    "healthcare",           # clinical, health records, biotech tooling
    "consumer",             # B2C apps, social, media, entertainment
    "marketplace",          # two-sided marketplaces, commerce
    "b2b_saas",             # general business software
    "hardware",             # physical products, robotics, devices
    "gaming",
    "education",
    "govtech",
    "climate",
)

TECHNICAL_DEPTH = (
    "low",      # business/GTM-facing; no technical fluency demanded
    "medium",   # works closely with engineering, reads specs, understands systems
    "high",     # APIs/SQL/architecture expected, or an engineering background asked for
)

SPONSORSHIP = (
    "offered",      # JD says sponsorship is available
    "not_offered",  # JD says no sponsorship, or requires citizenship/clearance
    "unstated",     # silent -- the common case
)

CONFIDENCE = ("high", "moderate", "low")


def is_valid_domain(tag: str) -> bool:
    return tag in DOMAIN_TAGS


# Values the model has actually produced that are recognisably one of ours.
# A tool-schema `enum` is a strong hint, not an enforced constraint -- observed
# live: the model answered "product_operations" where the vocabulary says
# "operations". Unmapped, that violates the job_facts check constraint and the
# whole batch fails on insert.
_REASON_ALIASES = {
    "product_operations": "operations",
    "product_ops": "operations",
    "ops": "operations",
    "program": "program_management",
    "tpm": "program_management",
    "marketing": "product_marketing",
    "pmm": "product_marketing",
    "solutions": "solutions_or_fde",
    "fde": "solutions_or_fde",
    "forward_deployed": "solutions_or_fde",
    "presales": "solutions_or_fde",
    "sales": "solutions_or_fde",
    "product_design": "design",
    "ux": "design",
    "product_engineering": "engineering",
    "software_engineering": "engineering",
    "data": "analytics",
    "product_analytics": "analytics",
    "director_plus": "leadership",
    "executive": "leadership",
}


def clean_exclusion_reason(reason) -> str:
    """Map a model-supplied reason onto the vocabulary, falling back to 'other'.

    Never raises and never returns something the check constraint would reject.
    """
    r = (reason or "").strip().lower().replace("-", "_").replace(" ", "_")
    if r in EXCLUSION_REASONS:
        return r
    return _REASON_ALIASES.get(r, "other")


def clean_domains(tags) -> list[str]:
    """Drop anything the model invented outside the vocabulary.

    A free-text tag would fragment the filter facets silently, which is exactly
    the failure a controlled vocabulary exists to prevent.
    """
    if not tags:
        return []
    seen, out = set(), []
    for t in tags:
        t = (t or "").strip().lower().replace("-", "_").replace(" ", "_")
        if t in DOMAIN_TAGS and t not in seen:
            seen.add(t)
            out.append(t)
    return out
