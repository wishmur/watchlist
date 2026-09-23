#!/usr/bin/env python3
"""
ingest.py
---------
ATS fan-out into Supabase. Makes ZERO LLM calls -- that is Gate 2's job, in
classify.py. Keeping the two apart is what makes the spend cap meaningful: this
stage can run as often as you like and costs nothing but HTTP.

What it does per company:
  1. list postings (one request)
  2. Gate 1: title + location screen, deterministic
  3. hydrate survivors (JD, offices, resolved multi-location)
  4. compute content_hash, resolve board scope, upsert jobs
  5. closed-detection -- only if the fetch actually succeeded

Closed-detection is the part worth being careful about
------------------------------------------------------
The previous implementation marked every job not seen in a run as closed, and
every fetcher swallowed errors to an empty list. A single transient 500 from
Greenhouse therefore closed that company's entire board. Here a fetch failure
is explicit (FetchResult.ok), closed-detection is skipped entirely for a failed
company, and a job must be missing from CLOSE_AFTER_MISSES consecutive
SUCCESSFUL fetches before it is closed. A job that reappears resets the counter.

Usage:
    python3 scripts/ingest.py --dry-run
    python3 scripts/ingest.py --ats greenhouse ashby
    python3 scripts/ingest.py --limit 50
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import local_env  # noqa: F401  -- loads .env for local runs

import ats
from filters import classify_title, resolve_scope

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("ingest")

SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or ""

# A company already in the DB only refetches its recent postings; a brand-new
# company seeds a back-catalogue.
#
# FIRST_RUN_DAYS is deliberately generous, and it is NOT the 7-day launch
# backfill window -- those are different things and conflating them starves the
# corpus. Measured on live boards, a 7-day first-run window captures 0 of
# Databricks' 24 open PM roles, 0 of Ramp's 6 and 1 of Spotify's 8, because
# companies do not post in the week they happen to be discovered. The next run
# treats them as "known" and drops to CUTOFF_HOURS, so that back-catalogue is
# lost permanently.
#
# Ingesting wide is free: this stage makes no LLM calls. Spend is controlled in
# classify.py by the call cap and newest-first ordering, and staleness is a
# display concern the board handles by sorting and filtering on posted_at --
# not something to enforce by throwing data away at fetch time.
CUTOFF_HOURS = int(os.getenv("CUTOFF_HOURS", "26"))
FIRST_RUN_DAYS = int(os.getenv("FIRST_RUN_DAYS", "60"))
CLOSE_AFTER_MISSES = int(os.getenv("CLOSE_AFTER_MISSES", "2"))
MAX_JD_CHARS = int(os.getenv("MAX_JD_CHARS", "20000"))


def _headers() -> dict:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


def sb_get(path: str, params: str = "") -> list[dict]:
    out, offset = [], 0
    while True:
        r = httpx.get(f"{SUPABASE_URL}/rest/v1/{path}?{params}&offset={offset}&limit=1000",
                      headers=_headers(), timeout=60)
        r.raise_for_status()
        batch = r.json()
        out += batch
        if len(batch) < 1000:
            return out
        offset += 1000


def sb_upsert(table: str, rows: list[dict], on_conflict: str) -> None:
    if not rows:
        return
    r = httpx.post(f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}",
                   headers={**_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
                   json=rows, timeout=90)
    r.raise_for_status()


def sb_patch(table: str, params: str, payload: dict) -> None:
    r = httpx.patch(f"{SUPABASE_URL}/rest/v1/{table}?{params}",
                    headers={**_headers(), "Prefer": "return=minimal"},
                    json=payload, timeout=60)
    r.raise_for_status()


def content_hash(title: str, jd: str) -> str:
    """Identifies the same real posting regardless of which ATS row it arrived as.

    Cross-posted requisitions (one company, same req, 14 cities) collapse to a
    single hash, so Gate 2 extracts once and every clone shares the result.
    """
    basis = f"{(title or '').strip().lower()}|{(jd or '').strip()}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def is_recent(posted_at: Optional[str], cutoff: Optional[datetime]) -> bool:
    if cutoff is None or not posted_at:
        return True
    try:
        dt = datetime.fromisoformat(str(posted_at).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt >= cutoff
    except Exception:
        return True


def process_company(co: dict, seen_before: bool, dry: bool) -> dict:
    """Returns per-company counters; never raises on ATS failure."""
    stats = {"listed": 0, "kept": 0, "upserted": 0, "failed": 0, "closed": 0}
    atsname, slug = co["ats_type"], co["ats_slug"]

    res = ats.list_postings(atsname, slug)
    if not res.ok:
        log.warning(f"{co['name']} [{atsname}]: fetch FAILED -- {res.error[:110]}")
        log.warning(f"{co['name']}: skipping closed-detection (a failed fetch is not an empty board)")
        stats["failed"] = 1
        return stats

    stats["listed"] = len(res.postings)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=CUTOFF_HOURS)) if seen_before \
        else (datetime.now(timezone.utc) - timedelta(days=FIRST_RUN_DAYS))

    survivors = []
    for p in res.postings:
        verdict = classify_title(p["title"])
        if verdict.decision == "exclude":
            continue
        # Pre-hydration location may be opaque ("3 Locations"); keep it and let
        # hydration resolve it rather than dropping on an unresolvable string.
        scope = resolve_scope(p["locations"])
        if not scope.in_scope and not scope.needs_resolution:
            continue
        if not is_recent(p.get("posted_at"), cutoff):
            continue
        survivors.append((p, verdict))

    rows = []
    for p, verdict in survivors:
        if ats.needs_hydration(atsname):
            p = ats.hydrate(atsname, slug, p)
            time.sleep(0.15)
        scope = resolve_scope(p["locations"])
        if not scope.in_scope:
            continue   # resolved out of the seven countries
        stats["kept"] += 1
        rows.append({
            "company_id": co["id"],
            "ats_job_id": p["ats_job_id"],
            "title": p["title"],
            "location": (p["locations"] or [""])[0],
            "locations": p["locations"],
            "countries": list(scope.countries),
            "board_scope": scope.scope,
            "remote_region": scope.remote_region,
            "url": p["url"],
            "posted_at": p.get("posted_at"),
            "raw_jd": (p.get("raw_jd") or "")[:MAX_JD_CHARS],
            "content_hash": content_hash(p["title"], p.get("raw_jd") or ""),
            "department": p.get("department"),
            "team": p.get("team"),
            "employment_type": p.get("employment_type"),
            "workplace_type": p.get("workplace_type"),
            "is_remote": p.get("is_remote"),
            "comp_min": p.get("comp_min"),
            "comp_max": p.get("comp_max"),
            "comp_currency": p.get("comp_currency"),
            "title_decision": verdict.decision,
            "title_reason": verdict.reason,
            "seniority": verdict.seniority,
            "status": "open",
            "missed_runs": 0,
            "last_seen_at": datetime.now(timezone.utc).isoformat(),
        })

    if dry:
        stats["upserted"] = len(rows)
        return stats

    sb_upsert("jobs", rows, "company_id,ats_job_id")
    stats["upserted"] = len(rows)

    # Closed-detection: only reachable when the fetch succeeded.
    live_ids = {p["ats_job_id"] for p in res.postings}
    existing = sb_get("jobs", f"select=id,ats_job_id,missed_runs&company_id=eq.{co['id']}&status=eq.open")
    missing = [j for j in existing if j["ats_job_id"] not in live_ids]
    to_close, to_bump = [], []
    for j in missing:
        if j.get("missed_runs", 0) + 1 >= CLOSE_AFTER_MISSES:
            to_close.append(j["id"])
        else:
            to_bump.append(j["id"])
    if to_bump:
        sb_patch("jobs", f"id=in.({','.join(to_bump)})", {"missed_runs": 1})
    if to_close:
        sb_patch("jobs", f"id=in.({','.join(to_close)})",
                 {"status": "closed", "closed_at": datetime.now(timezone.utc).isoformat()})
        stats["closed"] = len(to_close)
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ats", nargs="+", default=list(ats.SUPPORTED), choices=list(ats.SUPPORTED))
    ap.add_argument("--limit", type=int, default=0, help="max companies this run (0 = all)")
    ap.add_argument("--dry-run", action="store_true", help="fetch and screen, write nothing")
    args = ap.parse_args()

    if not SUPABASE_URL or not SUPABASE_KEY:
        log.error("SUPABASE_URL and SUPABASE_SERVICE_KEY are required")
        return 2

    if not args.dry_run:
        from check_schema import require
        require(SUPABASE_URL, SUPABASE_KEY, "jobs",
                ["content_hash", "locations", "countries", "board_scope", "missed_runs"],
                "sql/008_pm_board_v1.sql")

    companies = sb_get("companies", "select=id,name,ats_type,ats_slug&active=eq.true")
    companies = [c for c in companies if c["ats_type"] in args.ats]
    known = {j["company_id"] for j in sb_get("jobs", "select=company_id")}

    # New companies first, then known -- both shuffled, so a run that hits the
    # workflow timeout doesn't always starve the same alphabetical tail.
    fresh = [c for c in companies if c["id"] not in known]
    seen = [c for c in companies if c["id"] in known]
    random.shuffle(fresh)
    random.shuffle(seen)
    ordered = fresh + seen
    if args.limit:
        ordered = ordered[:args.limit]

    log.info(f"{len(ordered)} companies ({len(fresh)} new, {len(seen)} known) "
             f"across {', '.join(args.ats)}"
             + ("  [DRY RUN]" if args.dry_run else ""))

    totals = {"listed": 0, "kept": 0, "upserted": 0, "failed": 0, "closed": 0}
    started = time.time()
    for n, co in enumerate(ordered, 1):
        s = process_company(co, co["id"] in known, args.dry_run)
        for k in totals:
            totals[k] += s[k]
        if n % 50 == 0:
            log.info(f"  {n}/{len(ordered)} companies  kept={totals['kept']}  "
                     f"failed={totals['failed']}")
        time.sleep(0.2)

    elapsed = int(time.time() - started)
    log.info(f"done in {elapsed}s: listed={totals['listed']} kept={totals['kept']} "
             f"upserted={totals['upserted']} closed={totals['closed']} "
             f"failed_companies={totals['failed']}")
    if totals["failed"]:
        log.info(f"{totals['failed']} companies failed to fetch; their existing jobs were "
                 f"left open rather than being closed on a transient error")

    if not args.dry_run:
        try:
            sb_upsert("pipeline_runs", [{
                "stage": "ingest",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "companies_seen": len(ordered),
                "companies_failed": totals["failed"],
                "postings_listed": totals["listed"],
                "jobs_upserted": totals["upserted"],
                "jobs_closed": totals["closed"],
            }], "id")
        except Exception as e:
            log.warning(f"could not record pipeline_run: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
