#!/usr/bin/env python3
"""
expand_companies.py
--------------------
Wide-net company discovery. Reads the bundled Greenhouse / Ashby / Lever /
Workday slug lists in data/ (~12,500 companies total), skips anything
already in your Supabase `companies` table, then live-checks each
remaining candidate's job board for an actual open PM or FDE role (reusing
the same title/location filter as fetch_and_score.py). Only companies with a
real, current PM/FDE opening get written out — no guessing by name.

This makes zero Claude API calls, so discovery is free regardless of how
wide you set --limit. The only cost this feeds is downstream: every
company written to new_companies.sql becomes a company the daily cron
scores new postings for — which is exactly the set you want, since each
one is already confirmed to have a live PM opening today.

Usage:
  python expand_companies.py                              # all 4 ATS types, full local list
  python expand_companies.py --ats greenhouse
  python expand_companies.py --ats ashby lever --limit 1500 --tier explore
  python expand_companies.py --ats workday --concurrency 15

Env vars: SUPABASE_URL, SUPABASE_SERVICE_KEY (same as fetch_and_score.py)
Output:   new_companies.sql — review it, then run in the Supabase SQL editor
"""

import argparse, asyncio, csv, os, re, sys
import httpx

# Title classification (PM + FDE families) and the US-wide location filter are
# imported from filters.py so discovery and the daily scorer use identical rules.
from filters import classify_role, is_us_location

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
WORKDAY_URL_RE = re.compile(r"^https://([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/([\w./-]+)$", re.I)

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]


def existing_slugs() -> set[tuple[str, str]]:
    r = httpx.get(
        f"{SUPABASE_URL}/rest/v1/companies",
        headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
        params={"select": "ats_type,ats_slug"},
        timeout=20,
    )
    r.raise_for_status()
    return {(c["ats_type"], c["ats_slug"]) for c in r.json()}


def upsert_companies(rows: list[dict]) -> int:
    """Writes discovered companies straight to Supabase. Idempotent via
    on_conflict — safe to run daily without re-checking what's already there
    (existing_slugs() already filters those out before we get here anyway)."""
    if not rows:
        return 0
    r = httpx.post(
        f"{SUPABASE_URL}/rest/v1/companies",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
        params={"on_conflict": "ats_type,ats_slug"},
        json=rows,
        timeout=30,
    )
    r.raise_for_status()
    return len(rows)


def _load_csv(ats_type: str) -> list[dict]:
    path = os.path.join(DATA_DIR, f"{ats_type}.csv")
    if not os.path.exists(path):
        sys.exit(f"Missing data file: {path}\nExpected data/{ats_type}.csv bundled in the repo.")
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def candidates(ats_type: str, limit: int, skip: set) -> list[tuple[str, str]]:
    """Returns [(name, slug), ...] for this ATS, minus ones already in Supabase.
    slug is already normalized to the ats_slug format each fetcher expects."""
    rows = _load_csv(ats_type)
    out = []
    for row in rows:
        name = row["name"].strip()

        if ats_type == "workday":
            m = WORKDAY_URL_RE.match(row["url"].strip())
            if not m:
                continue  # skip anything that doesn't match the standard myworkdayjobs.com pattern
            tenant, wd_host, site = m.groups()
            slug = f"{wd_host}/{tenant}/{site}"
        else:
            slug = row["slug"].strip()

        if (ats_type, slug) not in skip:
            out.append((name, slug))

        if limit and len(out) >= limit:
            break
    return out


async def check_greenhouse(client, slug, sem):
    async with sem:
        try:
            r = await client.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=15)
            if r.status_code != 200:
                return None
            postings = r.json().get("jobs", [])
        except Exception:
            return None
        for p in postings:
            title = p.get("title") or ""
            loc = (p.get("location") or {}).get("name", "")
            if classify_role(title) is not None and is_us_location(loc):
                return title
        return None


async def check_ashby(client, slug, sem):
    # Ashby's posting-api response moved from {"jobPostings": [...], job.locationName}
    # to {"jobs": [...], job.location} — see the matching fix in fetch_and_score.py.
    async with sem:
        try:
            r = await client.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout=15)
            if r.status_code != 200:
                return None
            postings = r.json().get("jobs", [])
        except Exception:
            return None
        for p in postings:
            title = p.get("title") or ""
            loc = p.get("location", "")
            if classify_role(title) is not None and is_us_location(loc):
                return title
        return None


async def check_lever(client, slug, sem):
    async with sem:
        try:
            r = await client.get(f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=15)
            if r.status_code != 200:
                return None
            postings = r.json()
            if not isinstance(postings, list):
                return None
        except Exception:
            return None
        for p in postings:
            title = p.get("text") or ""
            loc = (p.get("categories") or {}).get("location", "")
            if classify_role(title) is not None and is_us_location(loc):
                return title
        return None


async def check_workday(client, slug, sem):
    """slug is 'wd{N}/{tenant}/{site}'. Only checks the first page (20 postings) —
    enough to detect whether a PM role exists without paginating every tenant."""
    async with sem:
        try:
            wd_host, tenant, site = slug.split("/", 2)
        except ValueError:
            return None
        api_url = f"https://{tenant}.{wd_host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
        try:
            r = await client.post(
                api_url,
                json={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""},
                headers={"content-type": "application/json"},
                timeout=15,
            )
            if r.status_code != 200:
                return None
            postings = r.json().get("jobPostings", [])
        except Exception:
            return None
        for p in postings:
            title = p.get("title") or ""
            loc = p.get("locationsText", "")
            if classify_role(title) is not None and is_us_location(loc):
                return title
        return None


CHECKERS = {
    "greenhouse": check_greenhouse,
    "ashby":      check_ashby,
    "lever":      check_lever,
    "workday":    check_workday,
}


async def scan(ats_type: str, cands: list[tuple[str, str]], concurrency: int):
    """Runs the live checks and returns [(name, ats_type, slug, title), ...] for matches."""
    sem = asyncio.Semaphore(concurrency)
    checker = CHECKERS[ats_type]
    results = []
    async with httpx.AsyncClient() as client:
        async def run_one(name, slug):
            title = await checker(client, slug, sem)
            return (name, slug, title)

        tasks = [run_one(name, slug) for name, slug in cands]
        done = 0
        for coro in asyncio.as_completed(tasks):
            name, slug, title = await coro
            done += 1
            if title:
                results.append((name, ats_type, slug, title))
            if done % 500 == 0:
                print(f"  {ats_type}: checked {done}/{len(cands)}, {len(results)} matches so far")
    return results


def esc(s: str) -> str:
    return s.replace("'", "''")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ats", nargs="+", default=["greenhouse", "ashby", "lever", "workday"])
    ap.add_argument("--limit", type=int, default=0,
                     help="candidates to check per ATS (0 = full local list, ~2-5k per type)")
    ap.add_argument("--concurrency", type=int, default=20,
                     help="parallel requests per ATS type; lower this for workday (heavier POST calls)")
    ap.add_argument("--tier", default="explore", choices=["dream", "strong", "explore"])
    ap.add_argument("--dry-run", action="store_true",
                     help="write new_companies.sql for review instead of inserting into Supabase")
    args = ap.parse_args()

    skip = existing_slugs()
    print(f"{len(skip)} companies already in Supabase — skipping those")

    all_matches = []
    for ats_type in args.ats:
        cands = candidates(ats_type, args.limit, skip)
        print(f"{ats_type}: {len(cands)} new candidates to live-check")
        matches = asyncio.run(scan(ats_type, cands, args.concurrency))
        print(f"{ats_type}: {len(matches)} companies have a live PM match")
        all_matches.extend(matches)

    if not all_matches:
        print("No new matches found.")
        return

    # Always write the audit log — cheap, and it's your paper trail for what
    # got added and why, even when writing straight to Supabase below.
    with open("new_companies.sql", "w") as f:
        f.write("-- Auto-discovered via expand_companies.py.\n")
        f.write("-- Each row had at least one open role matching your PM/FDE + US-location filters at scan time.\n")
        f.write("insert into companies (name, ats_type, ats_slug, tier, active, source, notes) values\n")
        rows = [
            f"  ('{esc(name)}', '{ats_type}', '{esc(slug)}', '{args.tier}', true, 'discovered', "
            f"'auto-discovered: {esc(title[:60])}')"
            for name, ats_type, slug, title in all_matches
        ]
        f.write(",\n".join(rows))
        f.write("\non conflict (ats_type, ats_slug) do nothing;\n")
    print(f"\nWrote {len(all_matches)} candidate companies to new_companies.sql (audit log)")

    if args.dry_run:
        print("--dry-run set: nothing written to Supabase. Review new_companies.sql, then paste it in yourself.")
        return

    rows = [
        {
            "name": name, "ats_type": ats_type, "ats_slug": slug,
            "tier": args.tier, "active": True, "source": "discovered",
            "notes": f"auto-discovered: {title[:60]}",
        }
        for name, ats_type, slug, title in all_matches
    ]
    written = upsert_companies(rows)
    print(f"Inserted/updated {written} companies directly in Supabase (tier={args.tier}, active=true).")
    print("They'll be picked up by tomorrow's daily fetch_and_score run automatically.")


if __name__ == "__main__":
    main()
