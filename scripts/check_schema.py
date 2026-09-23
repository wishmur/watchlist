#!/usr/bin/env python3
"""
check_schema.py
---------------
Asserts that the live Supabase project actually matches what the code and the
frontend expect. Read-only: it issues SELECTs and never writes.

Why this exists
---------------
The two repos are deliberately separate (pipeline owns schema, frontend only
reads views), and the one real cost of that split is silent drift. It already
happened: README.md documented sql/005_public_read_hardening.sql as applied,
and it was not. The two aggregate views it creates did not exist, and anon
could still read companies/jobs/matches directly -- every score ever computed,
including sub-threshold rejects whose reasoning text is candidate-specific,
plus the full raw_jd of every posting.

Nobody noticed because nothing ever checked. A migration is not "done" when the
file is committed; it is done when the live project agrees.

Usage
-----
    python scripts/check_schema.py              # full check, exits non-zero on failure
    python scripts/check_schema.py --warn-only  # report but always exit 0

Needs SUPABASE_URL plus a key. Pass the service key to check everything; the
anon-exposure checks additionally need SUPABASE_ANON_KEY to be meaningful.
"""

import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import local_env  # noqa: F401,E402  -- loads .env for local runs

SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or ""
ANON_KEY = os.getenv("SUPABASE_ANON_KEY") or ""

# Relations the pipeline and frontend depend on, with the columns actually read.
# Keep this in step with src/lib/watchlist/queries.ts in the frontend repo.
REQUIRED = {
    "companies": ["id", "name", "ats_type", "ats_slug", "tier", "active"],
    "jobs": [
        "id", "company_id", "ats_job_id", "title", "location", "url",
        "status", "first_seen_at", "last_seen_at",
        # sql/008
        "content_hash", "locations", "countries", "board_scope", "remote_region",
        "department", "team", "employment_type", "workplace_type", "is_remote",
        "comp_min", "comp_max", "comp_currency",
        "title_decision", "title_reason", "seniority", "missed_runs", "closed_at",
    ],
    "job_facts": [
        "company_id", "content_hash", "is_pm_role", "exclusion_reason", "confidence",
        "seniority", "years_required_min", "technical_depth", "domain_tags",
        "sponsorship_mentioned", "taxonomy_version", "extraction_model", "extracted_at",
    ],
    "pipeline_runs": ["id", "stage", "started_at", "classify_calls", "estimated_usd",
                      "hit_call_cap", "hit_budget_cap"],
    "classification_eval_runs": ["id", "run_at", "golden_set_version", "taxonomy_version",
                                 "example_count", "precision", "recall"],
    # The board's public surface.
    "v_jobs_public": ["job_id", "company_name", "title", "job_url", "locations",
                      "countries", "board_scope", "seniority", "domain_tags",
                      "posted_at", "completeness"],
    "v_jobs_us": ["job_id", "board_scope"],
    "v_jobs_intl": ["job_id", "board_scope"],
    "v_board_meta": ["open_roles", "us_roles", "intl_roles", "companies"],
    "v_eval_latest": ["run_at", "precision", "recall"],
    # Legacy, still serving the old board until the cutover.
    "v_watchlist": [
        "job_id", "company_name", "job_title", "location", "job_url",
        "posted_at", "first_seen_at", "job_status", "score", "reasoning",
    ],
    "matches": ["id", "job_id", "score", "scored_at"],
}

# Relations anon must NOT be able to read. These carry either candidate-specific
# reasoning text or full JD bodies, and the public board is served by views.
ANON_MUST_NOT_READ = ["companies", "jobs", "matches"]

# Relations anon is expected to read (the entire intended public surface).
ANON_MUST_READ = ["v_jobs_us", "v_jobs_intl", "v_board_meta", "v_watchlist"]


def require(url: str, key: str, table: str, columns: list[str], migration: str) -> None:
    """Fail fast and legibly when a migration has not been applied.

    Imported by ingest.py and classify.py so they exit with a pointer to the
    missing migration instead of a PostgREST 400 traceback.
    """
    try:
        r = httpx.get(
            f"{url}/rest/v1/{table}?select={','.join(columns)}&limit=1",
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            timeout=20,
        )
    except Exception as e:
        raise SystemExit(f"Could not reach Supabase to verify schema: {e}")
    if r.status_code == 200:
        return
    detail = r.text[:200]
    raise SystemExit(
        f"\nSchema check failed: {table} is missing columns this script needs.\n"
        f"  expected: {', '.join(columns)}\n"
        f"  server:   HTTP {r.status_code} {detail}\n\n"
        f"Apply {migration} in the Supabase SQL editor first, then re-run.\n"
        f"Verify with: python scripts/check_schema.py\n"
    )


class Result:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.warnings: list[str] = []
        self.passes = 0

    def ok(self, msg: str) -> None:
        self.passes += 1
        print(f"  [ok]   {msg}")

    def fail(self, msg: str) -> None:
        self.failures.append(msg)
        print(f"  [FAIL] {msg}")

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)
        print(f"  [warn] {msg}")


def _get(rel: str, key: str, params: str = "select=*&limit=1") -> httpx.Response:
    return httpx.get(
        f"{SUPABASE_URL}/rest/v1/{rel}?{params}",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        timeout=20,
    )


def check_relations(r: Result) -> None:
    print("\nRelations and columns (service key):")
    for rel, cols in REQUIRED.items():
        try:
            resp = _get(rel, SERVICE_KEY, f"select={','.join(cols)}&limit=1")
        except Exception as e:
            r.fail(f"{rel}: request failed ({e})")
            continue
        if resp.status_code == 404:
            r.fail(f"{rel}: does not exist (404) -- a migration has not been applied")
        elif resp.status_code == 400:
            # PostgREST reports the offending column in its error body.
            r.fail(f"{rel}: column mismatch -- {resp.text[:160]}")
        elif resp.status_code != 200:
            r.fail(f"{rel}: HTTP {resp.status_code} -- {resp.text[:120]}")
        else:
            r.ok(f"{rel}: present with all {len(cols)} expected columns")


def check_anon_exposure(r: Result) -> None:
    print("\nPublic (anon) read surface:")
    if not ANON_KEY:
        r.warn("SUPABASE_ANON_KEY not set -- skipping anon exposure checks. "
               "These are the ones that catch sql/005-class drift; set it in CI.")
        return

    for rel in ANON_MUST_READ:
        try:
            resp = _get(rel, ANON_KEY)
        except Exception as e:
            r.fail(f"anon {rel}: request failed ({e})")
            continue
        if resp.status_code == 200:
            r.ok(f"anon can read {rel} (required by the frontend)")
        else:
            r.fail(f"anon cannot read {rel} (HTTP {resp.status_code}) -- the board will be empty")

    for rel in ANON_MUST_NOT_READ:
        try:
            resp = _get(rel, ANON_KEY)
        except Exception as e:
            r.fail(f"anon {rel}: request failed ({e})")
            continue
        if resp.status_code == 200 and resp.text.strip() not in ("[]", ""):
            r.fail(f"anon CAN read raw table {rel} -- sql/005 not applied. "
                   f"Anyone with the public key can query it directly.")
        else:
            r.ok(f"anon blocked from raw table {rel}")


def main() -> int:
    warn_only = "--warn-only" in sys.argv
    if not SUPABASE_URL or not SERVICE_KEY:
        print("SUPABASE_URL and SUPABASE_SERVICE_KEY are required.", file=sys.stderr)
        return 2

    print(f"Checking {SUPABASE_URL}")
    r = Result()
    check_relations(r)
    check_anon_exposure(r)

    print(f"\n{r.passes} passed, {len(r.failures)} failed, {len(r.warnings)} warning(s)")
    if r.failures:
        print("\nFailures:")
        for f in r.failures:
            print(f"  - {f}")
        if not warn_only:
            print("\nThe live schema does not match what the code expects. "
                  "Apply the missing migration before relying on this run.")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
