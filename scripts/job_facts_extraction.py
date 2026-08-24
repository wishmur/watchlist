#!/usr/bin/env python3
"""
job_facts_extraction.py
------------------------
Populates job_facts: a SINGLE, candidate-agnostic structured extraction per
distinct job, run once at ingestion regardless of how many user profiles
will later be matched against it. This is the piece that makes matching
cost independent of visitor count -- see sql/006_job_facts_and_profiles.sql.

Deliberately does NOT do what fetch_and_score.py's scorer did: it never
reasons about whether a specific candidate qualifies. It only states facts
that are explicitly evidenced in the JD text, and leaves a field null/low-
confidence rather than inferring -- see SYSTEM prompt below, which targets a
specific bug found in Phase 0 eval: the old scorer routinely inferred a high,
unstated experience bar from a title word ("Senior", "Staff") even though the
candidate profile explicitly said not to. Extraction asks a narrower,
independently-checkable question per field instead of one fused judgment.

One call per distinct (company, content_hash) pair -- duplicate cross-posted
reqs (the same role posted once per city) share a single extraction via the
content_hash computed here, so they can never receive different facts.

No two-stage cheap/expensive model split, unlike the old scorer: since this
now runs once per job ever (not once per job x user), the cost that
motivated the Haiku-screen/Sonnet-confirm split no longer applies, and that
split was itself the source of the Phase 0 "stage1 vs stage2 disagreement"
finding -- one accurate model call removes that inconsistency source rather
than tuning it.

Env vars (GitHub Actions secrets, same as fetch_and_score.py):
  SUPABASE_URL, SUPABASE_SERVICE_KEY, ANTHROPIC_API_KEY

Optional:
  EXTRACTION_MODEL   Model used for extraction (default: claude-sonnet-5)
  JD_MAX_CHARS       JD chars sent to the model (default: 6000 -- generous,
                      since this runs once per job rather than once per
                      score, unlike fetch_and_score.py's tighter 3000 cap)
"""
import hashlib
import json
import logging
import os
import re
import time
from typing import Optional

import httpx

from taxonomy import (
    TAXONOMY_VERSION, ROLE_ARCHETYPES, SENIORITY_SCOPES, ROLE_DOMAIN_TAGS,
    WORK_AUTH_CONSTRAINTS, CONFIDENCE_LEVELS,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("job_facts_extraction")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

EXTRACTION_MODEL = os.getenv("EXTRACTION_MODEL", "claude-sonnet-5")
JD_MAX_CHARS = int(os.getenv("JD_MAX_CHARS", "6000"))

HEADERS_SB = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type": "application/json",
}

# ── Content hashing ─────────────────────────────────────────────────────────
# Must exactly match the hash computed in sql/006_job_facts_and_profiles.sql's
# backfill (lower(trim(title)) || '|' || coalesce(raw_jd, ''), sha256) so a
# job upserted here lines up with rows already backfilled by the migration.


def content_hash(title: str, raw_jd: str) -> str:
    normalized = f"{(title or '').strip().lower()}|{raw_jd or ''}"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# ── Extraction ──────────────────────────────────────────────────────────────

SYSTEM = f"""You extract structured facts about a job posting. You are NOT
evaluating fit for any candidate -- no candidate exists yet. State only what
the text explicitly supports. Where the text doesn't say, use null (for
scalar fields) or leave an array empty, and reflect that in the relevant
confidence/evidence field. Do not guess or infer beyond the text.

CRITICAL -- this rule exists because an earlier version of this system got
it wrong systematically: do NOT infer years_required_min or seniority_scope
from a title word alone ("Senior", "Staff", "Sr.", "Lead"). Title words are
company-specific leveling conventions, not experience requirements. Only set
years_required_min from an explicit number in the JD body. Only set
seniority_scope to people_manager or director_plus if the JD explicitly
describes direct reports, hiring, or building/leading a team -- a "Staff" or
"Senior" IC role with no such language is ic_senior_staff_principal, not
people_manager, regardless of how senior-sounding the title is.

FIELDS:

role_archetype -- one of {ROLE_ARCHETYPES} or null if none fit.
  core_pm: general product management, any vertical.
  forward_deployed_builder: hands-on technical builder who architects/ships
    code embedded with customers (Forward Deployed Engineer, Applied AI/ML
    Engineer, Deployment Engineer, and similarly-scoped "Solutions Architect"
    titles where the JD describes owning architecture/design, not just
    presales or short-term delivery engagements).
  technical_delivery_or_presales: customer-facing technical role centered on
    presales, professional-services delivery, or technical relationship
    management rather than owning long-term architecture (Solutions
    Engineer, presales-flavored Solutions Architect, technical delivery
    consulting roles).
  partner_channel: partner/alliance/channel relationship roles.
  research_or_internal_engineering: research or internal-facing engineering
    with no customer-facing component, even if the title contains "Applied"
    or "Deployed" as a team/org name rather than a functional description.

role_archetype_confidence -- one of {CONFIDENCE_LEVELS}. Use "low" and fill
  classification_note whenever the title's keywords suggest one function but
  the JD body describes something else (e.g. a title containing "Forward
  Deployed" or "Applied Engineer" that actually describes a design,
  marketing, or non-technical role) -- this is a known, real failure mode of
  the upstream keyword-based title filter, so flag it rather than silently
  picking the archetype the title implies.

seniority_scope -- one of {SENIORITY_SCOPES} or null.
years_required_min -- integer or null, exactly as stated (the minimum years
  explicitly required, not "nice to have" bonus experience).
experience_adjacency_allowed -- true if the JD explicitly accepts adjacent,
  equivalent, or "related" experience in place of the specific function
  named in the title; false if it strictly requires that specific
  experience; null if the JD doesn't address it either way.
seniority_evidence -- short quote or paraphrase grounding the seniority_scope
  and years_required_min calls, or null if both are null.

role_domain_tags -- array, zero or more of {ROLE_DOMAIN_TAGS}. Tag what THIS
  ROLE's actual day-to-day work touches, not the company's industry (e.g. an
  "Applied AI Engineer" role at a fintech company still gets ai_ml_llm here
  even though the company is fintech -- company_industry captures that
  separately). public_sector_federal vs public_sector_state_local matters:
  federal/DoD-flavored public-sector work commonly requires clearance, SLED
  (state/local/education) generally does not -- tag based on which is
  actually described, don't default to federal just because "public sector"
  appears.
company_industry -- a short free-text label for the company's business
  (e.g. "fintech", "AI research lab", "developer tools"), independent of
  role_domain_tags.

location_countries -- array of country names explicitly named or clearly
  implied by listed cities/states (e.g. ["United States"]). Empty array if
  genuinely unstated.
remote_eligible -- true/false/null based on explicit remote/hybrid/onsite language.

work_auth_constraints -- array, zero or more of {WORK_AUTH_CONSTRAINTS}. Only
  include a constraint if the JD states it as an actual requirement for this
  role -- not a benefits-section mention of "we sponsor visas" (that is the
  ABSENCE of no_sponsorship_stated, not a constraint itself), and not a
  conditional/partial statement like "some roles on this team require
  clearance, though most do not" unless THIS specific posting's own text
  states the requirement applies to it.
work_auth_evidence -- short quote grounding any work_auth_constraints entries, or null.
"""

TOOL_SCHEMA = {
    "name": "extract_job_facts",
    "description": "Structured, candidate-agnostic facts extracted from a job posting.",
    "input_schema": {
        "type": "object",
        "properties": {
            "role_archetype": {"type": ["string", "null"], "enum": ROLE_ARCHETYPES + [None]},
            "role_archetype_confidence": {"type": "string", "enum": CONFIDENCE_LEVELS},
            "classification_note": {"type": ["string", "null"]},
            "seniority_scope": {"type": ["string", "null"], "enum": SENIORITY_SCOPES + [None]},
            "years_required_min": {"type": ["integer", "null"]},
            "experience_adjacency_allowed": {"type": ["boolean", "null"]},
            "seniority_evidence": {"type": ["string", "null"]},
            "role_domain_tags": {"type": "array", "items": {"type": "string", "enum": ROLE_DOMAIN_TAGS}},
            "company_industry": {"type": ["string", "null"]},
            "location_countries": {"type": "array", "items": {"type": "string"}},
            "remote_eligible": {"type": ["boolean", "null"]},
            "work_auth_constraints": {"type": "array", "items": {"type": "string", "enum": WORK_AUTH_CONSTRAINTS}},
            "work_auth_evidence": {"type": ["string", "null"]},
        },
        "required": [
            "role_archetype", "role_archetype_confidence", "seniority_scope",
            "years_required_min", "experience_adjacency_allowed",
            "role_domain_tags", "location_countries", "work_auth_constraints",
        ],
    },
}


def extract_job_facts(title: str, location: str, raw_jd: str) -> Optional[dict]:
    """One extraction call. Returns the structured facts dict, or None on failure."""
    prompt = f"Title: {title}\nLocation: {location}\n\nDescription:\n{(raw_jd or '')[:JD_MAX_CHARS]}"
    payload = {
        "model": EXTRACTION_MODEL,
        "max_tokens": 1024,
        "system": SYSTEM,
        "tools": [TOOL_SCHEMA],
        "tool_choice": {"type": "tool", "name": "extract_job_facts"},
        "messages": [{"role": "user", "content": prompt}],
    }
    if "haiku" not in EXTRACTION_MODEL:
        payload["thinking"] = {"type": "disabled"}
    try:
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json=payload,
            timeout=45,
        )
        r.raise_for_status()
        body = r.json()
        tool_use = next((b for b in body.get("content", []) if b.get("type") == "tool_use"), None)
        if not tool_use:
            log.warning(f"No tool_use block for '{title}'")
            return None
        return tool_use.get("input")
    except Exception as e:
        log.warning(f"Extraction failed for '{title}': {e}")
        return None


# ── Supabase helpers ────────────────────────────────────────────────────────

def sb_get(path: str, params: dict = None) -> list[dict]:
    r = httpx.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=HEADERS_SB, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def sb_upsert(table: str, rows: list[dict], on_conflict: str):
    if not rows:
        return
    r = httpx.post(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers={**HEADERS_SB, "Prefer": "resolution=merge-duplicates,return=minimal"},
        params={"on_conflict": on_conflict},
        json=rows,
        timeout=30,
    )
    if r.status_code not in (200, 201):
        log.error(f"Upsert {table} failed: {r.status_code} {r.text[:300]}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    log.info("=== job_facts extraction starting ===")

    jobs = sb_get("jobs", {"status": "eq.open", "select": "id,company_id,title,location,raw_jd,content_hash"})
    log.info(f"Loaded {len(jobs)} open jobs")

    existing = sb_get("job_facts", {"select": "company_id,content_hash,taxonomy_version"})
    already_current = {(r["company_id"], r["content_hash"]) for r in existing if r["taxonomy_version"] == TAXONOMY_VERSION}
    log.info(f"{len(already_current)} (company, content_hash) pairs already extracted at {TAXONOMY_VERSION}")

    # Dedup within this run too: multiple `jobs` rows can share a content_hash
    # (the exact cross-posted-per-city case content_hash exists to solve) --
    # extract each distinct (company_id, content_hash) at most once per run.
    seen_this_run = set()
    to_extract = []
    for j in jobs:
        ch = j.get("content_hash") or content_hash(j["title"], j.get("raw_jd", ""))
        key = (j["company_id"], ch)
        if key in already_current or key in seen_this_run:
            continue
        seen_this_run.add(key)
        to_extract.append({**j, "content_hash": ch})

    log.info(f"{len(to_extract)} distinct jobs need extraction")

    rows = []
    errors = 0
    for i, j in enumerate(to_extract):
        facts = extract_job_facts(j["title"], j.get("location", ""), j.get("raw_jd", ""))
        if facts is None:
            errors += 1
            time.sleep(0.5)
            continue
        rows.append({
            "company_id": j["company_id"],
            "content_hash": j["content_hash"],
            **facts,
            "taxonomy_version": TAXONOMY_VERSION,
            "extraction_model_version": EXTRACTION_MODEL,
        })
        if (i + 1) % 20 == 0:
            log.info(f"  {i+1}/{len(to_extract)} extracted, upserting batch...")
            sb_upsert("job_facts", rows, "company_id,content_hash")
            rows = []
        time.sleep(0.3)

    if rows:
        sb_upsert("job_facts", rows, "company_id,content_hash")

    log.info(f"=== Done. extracted={len(to_extract) - errors} errors={errors} ===")


if __name__ == "__main__":
    main()
