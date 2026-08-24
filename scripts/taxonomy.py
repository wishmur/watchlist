#!/usr/bin/env python3
"""
taxonomy.py
-----------
Single source of truth for the job_facts / user_profiles controlled
vocabulary, shared by job_facts_extraction.py and (documented, not enforced
in SQL) sql/006_job_facts_and_profiles.sql.

Deliberately plain Python lists, not a database-enforced enum: the taxonomy
is expected to evolve as real job/user data surfaces gaps (see the v1
sanity-check against the Phase 0 golden set + raw postings pool, which is
where this exact vocabulary came from). Bump TAXONOMY_VERSION whenever a
value is added, removed, or redefined, and re-run extraction on rows whose
job_facts.taxonomy_version is older -- there is no other migration needed.
"""

TAXONOMY_VERSION = "v1-2026-08"

# FUNCTION: what the role actually does day-to-day. Independent of
# seniority, domain, and industry -- see seniority/domain/industry below.
ROLE_ARCHETYPES = [
    "core_pm",
    "forward_deployed_builder",           # hands-on technical builder embedded with customers
    "technical_delivery_or_presales",     # technical delivery / professional services / presales SE
    "partner_channel",                    # partner, alliance, channel relationship roles
    "research_or_internal_engineering",   # research or internal-facing engineering, not customer-embedded
]

# SENIORITY / MANAGEMENT SCOPE: independent of function. A role's function
# (e.g. forward_deployed_builder) and whether it carries management scope
# are separate facts -- do not collapse "Manager, Forward Deployed
# Engineering" into a single archetype value; it's forward_deployed_builder
# *function* + people_manager *scope*.
SENIORITY_SCOPES = [
    "ic",
    "ic_senior_staff_principal",   # Staff/Principal/Lead/Senior IC -- no direct reports
    "people_manager",
    "director_plus",
]

# DOMAIN: what THIS role's work actually touches, independent of the
# company's industry (an AI-native role can sit inside a fintech company,
# e.g. Ramp's "Applied AI Engineer" -- see company_industry below).
ROLE_DOMAIN_TAGS = [
    "ai_ml_llm",
    "dev_tools_infra",
    "cloud_data_platform",
    "fintech_payments",
    "security_trust_safety",
    "enterprise_search_data",
    "public_sector_federal",        # correlates with requires_clearance -- kept distinct from state/local
    "public_sector_state_local",    # SLED -- generally does NOT carry federal clearance requirements
    "consumer",
    "vertical_saas_other",          # catch-all for verticals not worth their own tag yet (legal, health, etc.)
]

# INDUSTRY: the company's business. One value, denormalized onto job_facts
# for convenience; the companies table remains the source of truth.
# Free text in practice (not a fixed list) -- companies span too many
# industries to enumerate usefully, unlike the smaller ROLE_DOMAIN_TAGS set.

# WORK AUTH: raw constraints as stated in the JD. Deliberately NOT evaluated
# against any specific candidate at extraction time -- whether a given
# constraint is a blocker depends on the *user's* work_auth_status, which
# doesn't exist yet when a job is ingested. See user_profiles.work_auth_excludes.
WORK_AUTH_CONSTRAINTS = [
    "requires_us_citizenship",
    "requires_clearance",
    "requires_green_card",
    "no_sponsorship_stated",
]

# Confidence tag for role_archetype specifically, since it's the field most
# prone to keyword-filter false positives (Phase 0 found "Forward Deployed
# Product Designer" and "Forward Deployed Creative" both hit the FDE keyword
# filter despite being design roles).
CONFIDENCE_LEVELS = ["high", "moderate", "low"]

WORK_AUTH_STATUSES = [
    "us_citizen", "green_card", "h1b", "opt_stem", "other_visa", "no_restriction",
]
