#!/usr/bin/env python3
"""
classify.py
-----------
Gate 2: the only place in this system that calls an LLM.

One Haiku call per distinct posting content, keyed by (company_id,
content_hash), writing user-agnostic facts into job_facts. Visitors never
trigger this -- the frontend reads views, has no Anthropic key and no server
route that could reach one. The guarantee is structural, not a policy.

Why one call, not two
---------------------
The previous design screened with Haiku and confirmed with Sonnet, which made
sense when the decision was a nuanced fit judgement. "Is this a PM role, and
what are its facts" is a Haiku-grade task given a tool schema, and the second
call answered a question the first had already answered well. Escalation now
triggers on the model's own `confidence: low` rather than on a score threshold,
and only when CLASSIFY_ESCALATE is on.

Spend control
-------------
Three independent layers, because an unbounded discovery run is the real cost
risk here, not per-posting price:

  1. MAX_CLASSIFY_CALLS  -- hard call budget; the worklist is sorted
     newest-first and truncated, and the remainder waits for tomorrow.
  2. RUN_BUDGET_USD      -- token accounting against a price table, aborting
     mid-run when exceeded. Catches unusually long JDs that slip the call cap.
  3. pipeline_runs       -- both recorded, so spend is queryable rather than
     inferred from Actions logs.

Re-extraction
-------------
A row is reclassified when its taxonomy_version or extraction_model is stale.
This is the path the old design lacked entirely: nothing was ever re-scored, so
a prompt fix only ever affected postings first seen afterwards.

Usage:
    python3 scripts/classify.py                 # classify pending work
    python3 scripts/classify.py --dry-run       # cost estimate, no API calls
    python3 scripts/classify.py --limit 50
    python3 scripts/classify.py --restale       # include stale-version rows
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import Any, Optional

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import local_env  # noqa: F401  -- loads .env for local runs

import taxonomy
from filters import TAXONOMY_VERSION as GATE1_VERSION, classify_title

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("classify")

SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or ""
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY") or ""

MODEL = os.getenv("CLASSIFY_MODEL", "claude-haiku-4-5-20251001")
ESCALATION_MODEL = os.getenv("CLASSIFY_ESCALATION_MODEL", "claude-sonnet-5")
ESCALATE = os.getenv("CLASSIFY_ESCALATE", "").lower() in ("1", "true", "yes")

MAX_CALLS = int(os.getenv("MAX_CLASSIFY_CALLS", "1500"))
RUN_BUDGET_USD = float(os.getenv("RUN_BUDGET_USD", "3.00"))
JD_MAX_CHARS = int(os.getenv("JD_MAX_CHARS", "6000"))

# USD per million tokens. Used for the budget circuit-breaker, so an unknown
# model must fail loud rather than silently costing nothing on paper.
PRICES = {
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-sonnet-5":           (3.00, 15.00),
    "claude-opus-5":             (15.00, 75.00),
}

EXTRACTION_VERSION = f"{taxonomy.TAXONOMY_VERSION}+{GATE1_VERSION}"


# ── Tool schema ──────────────────────────────────────────────────────────────
# A tool schema rather than "reply with JSON" prose. The old scorer asked for
# free-form JSON and needed raw_decode plus fence-stripping because ~14% of
# replies carried trailing text. Schema-enforced output removes that failure
# mode and the parsing code that worked around it.

EXTRACT_TOOL = {
    "name": "record_job_facts",
    "description": "Record structured, candidate-agnostic facts about one job posting.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_pm_role": {
                "type": "boolean",
                "description": (
                    "True only for core product management: owning what gets built and why, "
                    "for a product or product area. Product Manager, Product Owner, Product "
                    "Lead and their seniority variants. False for program/project management, "
                    "product marketing, forward-deployed/solutions/presales/customer "
                    "engineering, design, engineering, analytics and product operations -- "
                    "even when the title contains the word 'product'."
                ),
            },
            "exclusion_reason": {
                "type": "string",
                "enum": list(taxonomy.EXCLUSION_REASONS),
                "description": "Why it is not a PM role. Omit when is_pm_role is true.",
            },
            "confidence": {"type": "string", "enum": list(taxonomy.CONFIDENCE)},
            "classification_note": {
                "type": "string",
                "description": "One short clause citing the JD language that decided it. Max 140 chars.",
            },
            "seniority": {"type": "string", "enum": list(taxonomy.SENIORITY)},
            "years_required_min": {
                "type": ["integer", "null"],
                "description": "Minimum years the JD literally states. Null if unstated. Never infer from the title.",
            },
            "years_required_max": {"type": ["integer", "null"]},
            "experience_adjacency_allowed": {
                "type": ["boolean", "null"],
                "description": "True when the JD accepts adjacent/equivalent experience ('PM or equivalent technical experience').",
            },
            "technical_depth": {"type": "string", "enum": list(taxonomy.TECHNICAL_DEPTH)},
            "domain_tags": {
                "type": "array",
                "items": {"type": "string", "enum": list(taxonomy.DOMAIN_TAGS)},
                "description": "What THIS ROLE's work touches, not the company's overall business. At most 3.",
            },
            "company_industry": {"type": ["string", "null"]},
            "sponsorship_mentioned": {"type": "string", "enum": list(taxonomy.SPONSORSHIP)},
            "comp_min": {"type": ["number", "null"], "description": "Annual base salary floor stated in the JD."},
            "comp_max": {"type": ["number", "null"]},
            "comp_currency": {"type": ["string", "null"], "description": "ISO 4217, e.g. USD, GBP, EUR."},
        },
        "required": ["is_pm_role", "confidence", "seniority", "technical_depth",
                     "domain_tags", "sponsorship_mentioned"],
    },
}

SYSTEM = """You classify job postings for a public Product Management job board.

You are not evaluating anyone's fit. There is no candidate. Extract only what the posting itself states, so the board can filter on it.

THE ONE QUESTION THAT MATTERS: is this core product management -- owning what gets built and why for a product or product area?

These are NOT product management, however the title is worded:
- Forward Deployed Engineer, Solutions Engineer/Architect, Sales Engineer, Customer/Implementation/Deployment Engineer, Applied AI Engineer. These ship and integrate software for customers. Customer-facing technical delivery is not product ownership.
- Technical Program Manager, Program Manager, Project Manager, Delivery Manager. Coordinating delivery across teams is not deciding what gets built.
- Product Marketing Manager. Positioning and launching an existing product is not owning it.
- Product Designer, Product Engineer, Product Analyst, Product Operations. The word "product" qualifies a different function.

A title can be misleading in both directions. "Forward Deployed Product Manager" may be a genuine PM role embedded with customers, or an FDE with a PM label -- read the responsibilities and decide. Equally, a posting titled "Product Manager" whose responsibilities are entirely presales demos is not a PM role.

Ground every field in what the JD says. If it does not state years of experience, return null rather than a guess -- an invented number is worse than a missing one, because the board filters on it. Set confidence "low" when the JD is too thin to judge rather than guessing confidently."""


# ── Supabase ─────────────────────────────────────────────────────────────────

def _headers() -> dict:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


def sb_get(path: str, params: str = "") -> list[dict]:
    out, offset = [], 0
    while True:
        url = f"{SUPABASE_URL}/rest/v1/{path}?{params}&offset={offset}&limit=1000"
        r = httpx.get(url, headers=_headers(), timeout=60)
        r.raise_for_status()
        batch = r.json()
        out += batch
        if len(batch) < 1000:
            return out
        offset += 1000


def sb_upsert(table: str, rows: list[dict], on_conflict: str) -> None:
    if not rows:
        return
    r = httpx.post(
        f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}",
        headers={**_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
        json=rows, timeout=90,
    )
    r.raise_for_status()


# ── Model call ───────────────────────────────────────────────────────────────

class Spend:
    def __init__(self) -> None:
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.usd = 0.0

    def add(self, model: str, usage: dict) -> None:
        self.calls += 1
        i = usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
        o = usage.get("output_tokens", 0)
        self.input_tokens += i
        self.output_tokens += o
        if model not in PRICES:
            raise RuntimeError(
                f"No price for model {model!r}; refusing to run an uncapped spend. "
                f"Add it to PRICES."
            )
        pi, po = PRICES[model]
        self.usd += (i / 1e6) * pi + (o / 1e6) * po


def call_model(model: str, title: str, locations: list[str], department: Optional[str],
               jd: str, spend: Spend) -> Optional[dict]:
    user = (
        f"Title: {title}\n"
        f"Locations: {', '.join(locations) if locations else 'unstated'}\n"
        f"ATS department: {department or 'unstated'}\n\n"
        f"Job description:\n{(jd or '')[:JD_MAX_CHARS] or '(none provided)'}"
    )
    body = {
        "model": model,
        "max_tokens": 1024,
        "system": SYSTEM,
        "tools": [EXTRACT_TOOL],
        "tool_choice": {"type": "tool", "name": "record_job_facts"},
        "messages": [{"role": "user", "content": user}],
    }
    for attempt in range(3):
        try:
            r = httpx.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=body, timeout=90,
            )
            if r.status_code in (429, 500, 502, 503, 529) and attempt < 2:
                time.sleep(2 ** attempt * 2)
                continue
            r.raise_for_status()
            data = r.json()
            spend.add(model, data.get("usage", {}))
            for block in data.get("content", []):
                if block.get("type") == "tool_use":
                    return block.get("input") or {}
            log.warning(f"no tool_use block for {title[:40]!r}")
            return None
        except Exception as e:
            if attempt == 2:
                log.warning(f"model call failed for {title[:40]!r}: {e}")
                return None
            time.sleep(2 ** attempt * 2)
    return None


# ── Worklist ─────────────────────────────────────────────────────────────────

def _one_of(value, vocabulary, default=None):
    """Clamp a model-supplied value to a controlled vocabulary.

    Tool-schema enums are a strong hint, not a guarantee -- the model has been
    observed returning values outside them. Every enum-constrained column goes
    through here so an off-vocabulary answer degrades to a default instead of
    failing the whole batch on a check constraint.
    """
    v = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return v if v in vocabulary else default


def build_worklist(restale: bool, since_days: Optional[int] = None) -> list[dict]:
    """Distinct (company_id, content_hash) pairs needing extraction, newest first.

    Deduping by content_hash is what makes cross-posted requisitions free: one
    company posts the same req in 14 cities, and all 14 share one extraction.
    """
    log.info("loading open jobs ...")
    jobs = sb_get("jobs", "select=id,company_id,content_hash,title,locations,department,"
                          "raw_jd,posted_at,first_seen_at,title_decision&status=eq.open")

    # Rows written by the old PM+FDE pipeline carry title_decision = NULL, so a
    # plain `!= 'exclude'` test lets every Forward Deployed Engineer and
    # Solutions Engineer in the back catalogue through -- each one a paid call
    # for a role the board will never show. Re-run Gate 1 on anything unlabelled
    # rather than trusting a column the old pipeline never populated.
    before = len(jobs)
    kept = []
    for j in jobs:
        decision = j.get("title_decision") or classify_title(j["title"]).decision
        if decision != "exclude":
            kept.append(j)
    if before != len(kept):
        log.info(f"Gate 1 screened out {before - len(kept)} unlabelled legacy rows "
                 f"({len(kept)} remain)")
    jobs = kept

    facts = sb_get("job_facts", "select=company_id,content_hash,taxonomy_version,extraction_model")
    done = {}
    for f in facts:
        done[(f["company_id"], f["content_hash"])] = (f.get("taxonomy_version"), f.get("extraction_model"))

    work: dict[tuple, dict] = {}
    stale = 0
    for j in jobs:
        key = (j["company_id"], j.get("content_hash"))
        if not key[1]:
            continue
        if key in done:
            tv, em = done[key]
            if not restale or (tv == EXTRACTION_VERSION and em == MODEL):
                continue
            stale += 1
        prev = work.get(key)
        if prev is None or (j.get("posted_at") or "") > (prev.get("posted_at") or ""):
            work[key] = j

    ordered = sorted(work.values(),
                     key=lambda j: (j.get("posted_at") or j.get("first_seen_at") or ""),
                     reverse=True)

    # The 7-day backfill window belongs here, not in ingest.py: this is the only
    # stage that costs money, so bounding it here bounds spend without throwing
    # away corpus. Launch runs with --since-days 7; steady state leaves it off,
    # because by then the only unclassified rows are new anyway.
    if since_days:
        cut = time.strftime("%Y-%m-%d", time.gmtime(time.time() - since_days * 86400))
        before = len(ordered)
        ordered = [j for j in ordered
                   if (j.get("posted_at") or j.get("first_seen_at") or "")[:10] >= cut]
        log.info(f"--since-days {since_days}: {before} -> {len(ordered)} postings (posted on/after {cut})")

    log.info(f"{len(jobs)} open jobs -> {len(ordered)} distinct postings to classify"
             + (f" ({stale} stale re-extractions)" if stale else ""))
    return ordered


def estimate_usd(n: int) -> float:
    pi, po = PRICES[MODEL]
    # Measured against a real 639-call production run: $3.0017 total, so
    # ~$0.0047 per call. An earlier 2.0k-in/250-out guess predicted $0.0023 and
    # understated a full run by about half -- real postings carry longer JDs
    # than the estimate assumed. Numbers below are back-solved from that run.
    return n * ((4200 / 1e6) * pi + (330 / 1e6) * po)


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="estimate cost, make no API calls")
    ap.add_argument("--limit", type=int, default=None, help="override MAX_CLASSIFY_CALLS")
    ap.add_argument("--restale", action="store_true", help="re-extract stale taxonomy/model rows")
    ap.add_argument("--since-days", type=int, default=None,
                    help="only classify postings this recent (launch backfill uses 7)")
    args = ap.parse_args()

    if not SUPABASE_URL or not SUPABASE_KEY:
        log.error("SUPABASE_URL and SUPABASE_SERVICE_KEY are required")
        return 2

    from check_schema import require
    require(SUPABASE_URL, SUPABASE_KEY, "jobs",
            ["content_hash", "locations", "department", "title_decision"],
            "sql/008_pm_board_v1.sql")
    require(SUPABASE_URL, SUPABASE_KEY, "job_facts",
            ["is_pm_role", "taxonomy_version", "extraction_model"],
            "sql/008_pm_board_v1.sql")

    cap = args.limit if args.limit is not None else MAX_CALLS
    work = build_worklist(args.restale, args.since_days)
    truncated = len(work) > cap
    if truncated:
        log.info(f"call cap {cap}: classifying the {cap} newest, "
                 f"{len(work) - cap} deferred to the next run")
        work = work[:cap]

    if args.dry_run:
        log.info(f"DRY RUN: {len(work)} postings, estimated ${estimate_usd(len(work)):.2f} "
                 f"with {MODEL} (budget ${RUN_BUDGET_USD:.2f})")
        for j in work[:8]:
            v = classify_title(j["title"])
            log.info(f"   [{v.decision}/{v.seniority}] {j['title'][:60]}")
        return 0

    if not ANTHROPIC_KEY:
        log.error("ANTHROPIC_API_KEY is required")
        return 2

    spend = Spend()
    rows: list[dict] = []
    hit_budget = False
    started = time.time()

    for n, j in enumerate(work, 1):
        if spend.usd >= RUN_BUDGET_USD:
            log.warning(f"budget ${RUN_BUDGET_USD:.2f} reached after {n-1} postings; stopping")
            hit_budget = True
            break

        out = call_model(MODEL, j["title"], j.get("locations") or [],
                         j.get("department"), j.get("raw_jd") or "", spend)
        model_used = MODEL
        if out and ESCALATE and out.get("confidence") == "low":
            better = call_model(ESCALATION_MODEL, j["title"], j.get("locations") or [],
                                j.get("department"), j.get("raw_jd") or "", spend)
            if better:
                out, model_used = better, ESCALATION_MODEL

        if not out:
            continue

        is_pm = bool(out.get("is_pm_role"))
        rows.append({
            "company_id": j["company_id"],
            "content_hash": j["content_hash"],
            "is_pm_role": is_pm,
            "exclusion_reason": None if is_pm else taxonomy.clean_exclusion_reason(out.get("exclusion_reason")),
            "confidence": _one_of(out.get("confidence"), taxonomy.CONFIDENCE, "moderate"),
            "classification_note": (out.get("classification_note") or "")[:200] or None,
            "seniority": _one_of(out.get("seniority"), taxonomy.SENIORITY, None),
            "years_required_min": out.get("years_required_min"),
            "years_required_max": out.get("years_required_max"),
            "experience_adjacency_allowed": out.get("experience_adjacency_allowed"),
            "technical_depth": _one_of(out.get("technical_depth"), taxonomy.TECHNICAL_DEPTH, None),
            "domain_tags": taxonomy.clean_domains(out.get("domain_tags")),
            "company_industry": (out.get("company_industry") or None),
            "sponsorship_mentioned": _one_of(out.get("sponsorship_mentioned"), taxonomy.SPONSORSHIP, "unstated"),
            "comp_min": out.get("comp_min"),
            "comp_max": out.get("comp_max"),
            "comp_currency": out.get("comp_currency"),
            "comp_source": "jd_text" if out.get("comp_min") else "none",
            "taxonomy_version": EXTRACTION_VERSION,
            "extraction_model": model_used,
        })

        if len(rows) >= 50:
            sb_upsert("job_facts", rows, "company_id,content_hash")
            rows = []
        if n % 100 == 0:
            log.info(f"  {n}/{len(work)}  ${spend.usd:.2f}  {spend.calls} calls")

    sb_upsert("job_facts", rows, "company_id,content_hash")

    elapsed = int(time.time() - started)
    log.info(f"done: {spend.calls} calls, {spend.input_tokens} in / {spend.output_tokens} out, "
             f"${spend.usd:.3f}, {elapsed}s")

    try:
        sb_upsert("pipeline_runs", [{
            "stage": "classify",
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
            "classify_calls": spend.calls,
            "input_tokens": spend.input_tokens,
            "output_tokens": spend.output_tokens,
            "estimated_usd": round(spend.usd, 4),
            "hit_call_cap": truncated,
            "hit_budget_cap": hit_budget,
        }], "id")
    except Exception as e:
        log.warning(f"could not record pipeline_run: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
