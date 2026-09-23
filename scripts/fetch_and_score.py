#!/usr/bin/env python3
"""
fetch_and_score.py
------------------
Pulls open jobs from each ATS, filters by PM/FDE title + US location + recency,
fetches the JD, and scores relevant jobs against the candidate profile in two
stages (cheap Haiku screen -> Sonnet confirm for the promising few), then upserts
matches into Supabase. The candidate profile + rubric ride in a cached system
block to keep per-call token cost down.

Title classification (PM + FDE families) and the US-wide location filter live in
filters.py, shared with expand_companies.py.

Env vars (GitHub Actions secrets):
  SUPABASE_URL          https://xxxx.supabase.co
  SUPABASE_SERVICE_KEY  service_role key
  ANTHROPIC_API_KEY     Claude API key

Optional (cost/behavior knobs):
  SCORE_THRESHOLD       Min score to store a match (default: 65)
  SCORE_MODEL_STAGE1    Cheap screening model (default: claude-haiku-4-5)
  SCORE_MODEL_STAGE2    Confirmation model (default: claude-sonnet-5)
  STAGE1_PASS           Haiku score that escalates to Sonnet (default: 55)
  JD_MAX_CHARS          JD chars sent to the model (default: 3000)
  FIRST_RUN_DAYS        Back-catalog window for a new company (default: 7)
"""

import os, json, time, logging, random, re
from datetime import datetime, timezone, timedelta
from typing import Optional
import httpx

# Role classification + US-location filters live in filters.py so they stay
# identical between this daily pipeline and expand_companies.py (discovery).
from filters import classify_role, is_us_location

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("watchlist")

# ── Config ─────────────────────────────────────────────────────────────────────
# Credentials are loaded lazily via _load_config() rather than at import time, so
# this module can be imported for its scoring functions (score_job, _call_model,
# _apply_score_guards -- used directly by scripts/run_eval.py) without requiring
# Supabase/Anthropic secrets to be present. main() calls _load_config() itself;
# any other caller that needs Supabase I/O or model calls must call it first.

SUPABASE_URL: Optional[str] = None
SUPABASE_SERVICE_KEY: Optional[str] = None
ANTHROPIC_API_KEY: Optional[str] = None
HEADERS_SB: Optional[dict] = None

SCORE_THRESHOLD      = int(os.getenv("SCORE_THRESHOLD", "65"))

# Two-stage scoring to keep cost down: a cheap Haiku pass screens every job, and
# only jobs that clear STAGE1_PASS get the authoritative (pricier) Sonnet verdict.
# ~97% of jobs are rejected, so this keeps the expensive model off the rejects.
SCORE_MODEL_STAGE1 = os.getenv("SCORE_MODEL_STAGE1", "claude-haiku-4-5")   # screen
SCORE_MODEL_STAGE2 = os.getenv("SCORE_MODEL_STAGE2", "claude-sonnet-5")    # confirm
STAGE1_PASS        = int(os.getenv("STAGE1_PASS", "55"))   # Haiku >= this -> Sonnet
JD_MAX_CHARS       = int(os.getenv("JD_MAX_CHARS", "3000"))  # JD chars sent to the model

# Per-run call counters, surfaced in the final log line for cost visibility.
SCORE_STATS = {
    "stage1": 0, "stage2": 0,
    # Cache diagnostics — Anthropic warns when cache_control is set but never
    # hits (e.g. our ~1,800-token prefix is below Haiku 4.5's 4,096-tok minimum
    # so Haiku silently doesn't cache). Track per-model so the summary shows
    # which stage is actually caching.
    "cache_reads": {},    # {model: total tokens read from cache}
    "cache_writes": {},   # {model: total tokens written to cache}
    "input_tokens": {},   # {model: total uncached input tokens billed at full price}
}

def _load_anthropic_key() -> None:
    """Load just ANTHROPIC_API_KEY -- everything score_job()/_call_model() need.
    Split out from _load_config() so a scoring-only caller (e.g. run_eval.py in
    --dry-run mode, before it decides whether it also needs to write results to
    Supabase) doesn't have to have Supabase secrets configured at all."""
    global ANTHROPIC_API_KEY
    ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]


def _load_supabase_config() -> None:
    """Load SUPABASE_URL/SUPABASE_SERVICE_KEY and build HEADERS_SB -- everything
    sb_get()/sb_upsert()/sb_patch() need."""
    global SUPABASE_URL, SUPABASE_SERVICE_KEY, HEADERS_SB
    SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
    SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
    HEADERS_SB = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }


def _load_config() -> None:
    """Load every required env var (GitHub Actions secrets, or exported locally).
    Raises KeyError with the missing var's name if one isn't set. Safe to call
    more than once. This is what main() (the daily pipeline) uses, since it needs
    both model calls and Supabase I/O."""
    _load_anthropic_key()
    _load_supabase_config()

# Recency windows:
#   - A company already in the DB fetches only its recent postings (~last 24h).
#   - A brand-new company (no jobs yet) seeds a 30-day back-catalog on first run.
# NOTE: postings with no date (all Workday, some others) can't be dated, so they
# are always kept regardless of either window.
CUTOFF_HOURS   = 26  # slightly over 24h to avoid missing jobs near the boundary
FIRST_RUN_DAYS = int(os.getenv("FIRST_RUN_DAYS", "7"))  # back-catalog window the first time a company is seen

# ── Candidate profile ──────────────────────────────────────────────────────────
# Lives in profile/candidate_profile.md so it can be edited without touching code.

_PROFILE_PATH = os.path.join(os.path.dirname(__file__), "..", "profile", "candidate_profile.md")
with open(_PROFILE_PATH) as _f:
    CANDIDATE_PROFILE = _f.read()

# ── Filters ────────────────────────────────────────────────────────────────────
# The PM/FDE title classifier (classify_role) and the US-wide location check
# (is_us_location) are imported from filters.py — the single source of truth
# shared with expand_companies.py.


def is_recent(posted_at: Optional[str], cutoff: datetime) -> bool:
    """Return True if posted_at is within the cutoff window, or if date is unknown."""
    if not posted_at:
        return True  # no date = keep it, we can't tell
    try:
        dt = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt >= cutoff
    except Exception:
        return True  # parse failure = keep it


def should_keep(title: str, location: str, posted_at: Optional[str], cutoff: Optional[datetime]) -> bool:
    if classify_role(title) is None:
        return False
    if not is_us_location(location):
        return False
    if cutoff and not is_recent(posted_at, cutoff):
        return False
    return True


# ── ATS Fetchers ───────────────────────────────────────────────────────────────

def fetch_greenhouse(slug: str) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    try:
        r = httpx.get(url, timeout=20)
        r.raise_for_status()
        return [
            {
                "ats_job_id": str(j["id"]),
                "title":      j.get("title", ""),
                "location":   (j.get("location") or {}).get("name", ""),
                "url":        j.get("absolute_url", ""),
                "posted_at":  j.get("updated_at"),
                "raw_jd":     "",
            }
            for j in r.json().get("jobs", [])
        ]
    except Exception as e:
        log.warning(f"Greenhouse {slug}: {e}")
        return []


def fetch_greenhouse_jd(job_id: str, slug: str) -> str:
    try:
        r = httpx.get(
            f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}",
            timeout=15,
        )
        r.raise_for_status()
        return _strip_html(r.json().get("content", ""))
    except Exception:
        return ""


def fetch_ashby(slug: str) -> list[dict]:
    # Ashby's posting-api returns {"jobs": [...]} with job.location -- it was once
    # {"jobPostings": [...]} with job.locationName. An unrecognized key yields []
    # rather than raising, so this failed silently: 21 seeded Ashby companies
    # produced 0 board rows while the logs looked clean.
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    try:
        r = httpx.get(url, timeout=20)
        r.raise_for_status()
        return [
            {
                "ats_job_id": j.get("id", ""),
                "title":      j.get("title", ""),
                "location":   j.get("location", ""),
                "url":        j.get("jobUrl", ""),
                "posted_at":  j.get("publishedAt"),
                "raw_jd":     _strip_html(j.get("descriptionHtml", "")),
            }
            for j in r.json().get("jobs", [])
            if j.get("isListed") is not False
        ]
    except Exception as e:
        log.warning(f"Ashby {slug}: {e}")
        return []


def fetch_lever(slug: str) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        r = httpx.get(url, timeout=20)
        r.raise_for_status()
        return [
            {
                "ats_job_id": j.get("id", ""),
                "title":      j.get("text", ""),
                "location":   (j.get("categories") or {}).get("location", ""),
                "url":        j.get("hostedUrl", ""),
                "posted_at":  datetime.fromtimestamp(
                                  j["createdAt"] / 1000, tz=timezone.utc
                              ).isoformat() if j.get("createdAt") else None,
                # Lever's list endpoint already includes the full JD, so no
                # per-posting call is needed (unlike Greenhouse/Workday).
                "raw_jd":     _strip_html(j.get("descriptionPlain") or j.get("description") or ""),
            }
            for j in r.json()
        ]
    except Exception as e:
        log.warning(f"Lever {slug}: {e}")
        return []


def fetch_workday(slug: str) -> list[dict]:
    """
    slug format: 'wd{N}/{tenant}/{site}', e.g. 'wd5/nvidia/nvidiaexternalcareersite'
    (see sql/seed.sql header for the format reminder).

    Workday has no public REST API. This calls the same undocumented JSON
    endpoint their own careers-page widget uses. Capped at 100 postings per
    company (5 pages) — plenty to find PM roles without hammering a tenant
    that has thousands of open reqs.
    """
    try:
        wd_host, tenant, site = slug.split("/", 2)
    except ValueError:
        log.warning(f"Workday slug malformed (expected wdN/tenant/site): {slug}")
        return []

    base = f"https://{tenant}.{wd_host}.myworkdayjobs.com"
    api_url = f"{base}/wday/cxs/{tenant}/{site}/jobs"

    postings, offset, limit, max_pages = [], 0, 20, 5
    try:
        for _ in range(max_pages):
            r = httpx.post(
                api_url,
                json={"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": ""},
                headers={"content-type": "application/json"},
                timeout=20,
            )
            r.raise_for_status()
            data = r.json()
            batch = data.get("jobPostings", [])
            if not batch:
                break
            for j in batch:
                ext_path = j.get("externalPath", "")
                job_id = ext_path.rsplit("_", 1)[-1] if ext_path else ext_path
                postings.append({
                    "ats_job_id": job_id or ext_path or j.get("title", ""),
                    "title":      j.get("title", ""),
                    "location":   j.get("locationsText", "") or "",
                    "url":        f"{base}/{site}{ext_path}" if ext_path else base,
                    # Workday only exposes relative strings ("Posted Today"), not
                    # real timestamps, so posted_at stays unknown -> is_recent() keeps it.
                    "posted_at":  None,
                    "raw_jd":     "",
                    # Kept for JD hydration in main(); ignored by the upsert.
                    "_ext_path":  ext_path,
                })
            offset += limit
            if offset >= data.get("total", 0):
                break
            time.sleep(0.15)
    except Exception as e:
        log.warning(f"Workday {slug}: {e}")
    return postings


def fetch_workday_jd(slug: str, ext_path: str) -> str:
    """Fetch a single Workday posting's full JD from the same cxs endpoint the
    careers widget uses. `slug` is 'wd{N}/{tenant}/{site}', `ext_path` is the
    posting's externalPath (starts with '/')."""
    if not ext_path:
        return ""
    try:
        wd_host, tenant, site = slug.split("/", 2)
    except ValueError:
        return ""
    url = f"https://{tenant}.{wd_host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{ext_path}"
    try:
        r = httpx.get(url, headers={"content-type": "application/json"}, timeout=15)
        r.raise_for_status()
        info = r.json().get("jobPostingInfo", {}) or {}
        return _strip_html(info.get("jobDescription", ""))
    except Exception:
        return ""


ATS_FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "ashby":      fetch_ashby,
    "lever":      fetch_lever,
    "workday":    fetch_workday,
}

# ── Claude Scoring ─────────────────────────────────────────────────────────────
# Models are configured above (SCORE_MODEL_STAGE1 / STAGE2).

SCORE_SYSTEM = """You are a job-fit evaluator for one specific candidate. Judge how well THIS candidate fits THIS role. Company prestige is irrelevant: a famous AI lab role that rigidly demands 8 years of product management is a BAD fit and must score low.

Output ONLY valid JSON (no markdown, no prose). Exactly these fields:
  score            integer 0-100
  role_family      "pm" | "fde" | "other"      (which lens you judged with)
  role_fit         "strong" | "moderate" | "weak"
  level_fit        "strong" | "moderate" | "weak"
  location_fit     "strong" | "moderate" | "weak"
  years_required   integer or null   (max years of experience the JD demands; null if unstated)
  meets_experience true | false      (does the candidate qualify under the candidate's years-of-experience rule?)
  blocker          true | false      (does it require US citizenship, a security clearance, a green card, or state no sponsorship ever?)
  reasoning        string, max 120 chars, plain English

Which lens to use (role_family):
  - "pm"  = Product Manager / Product Lead roles. Judge product-ownership fit.
  - "fde" = Forward Deployed / Applied AI / Solutions / Deployment / Implementation Engineer roles. Judge "engineer who ships next to the customer" fit (coding + customer delivery), NOT PM ownership.
  - "other" = neither. Set role_fit weak.
A title-screen guess is provided, but decide for yourself from the JD.

DIMENSION RUBRICS:

ROLE FIT (does the role type match a target family + the candidate's strengths?):
  strong:   A PM or FDE role squarely in the candidate's wheelhouse — AI/ML, LLM apps, developer tools, cloud/data platforms, workflow automation, applied-AI, or technical/platform PM.
  moderate: A legitimate PM/FDE role in a viable but less-preferred area (general B2B SaaS, fintech, edtech, consumer, marketplaces) the candidate could still do well in.
  weak:     Not really a target role — the title is actually EM, designer, analyst, program/project manager, or marketing (role_family "other") — or it demands deep domain expertise the candidate lacks.

LEVEL FIT (apply the candidate's years-of-experience rule literally):
  strong:   JD accepts "X years of PM OR equivalent / adjacent / technical / related experience," OR asks ~2-5 years, OR uses "mid-level"/"IC" language, OR is PM II/III. -> meets_experience true.
  moderate: JD asks ~5-7 years of product management, OR says "Senior" but the described scope is individual-contributor (no direct reports).
  weak:     JD rigidly requires 5+ years of PURE product management with no adjacency clause, OR requires 8+ years, OR requires people management, OR is Director/VP/Head-of level. -> meets_experience false.

LOCATION FIT:
  strong:   Any US location (any city or state), Remote/Hybrid with US eligibility, or unspecified. Never penalize a specific US city.
  moderate: US-eligible but ambiguous.
  weak:     Genuinely non-US, or requires work authorization / relocation outside the US.

BLOCKER: set blocker true only if the JD requires US citizenship, an active security clearance, a green card / permanent residency, or explicitly states no US visa sponsorship, ever, for a US-based role. The candidate is on OPT and cannot take those.
Do NOT set blocker for a non-US role's own-country work-authorization requirements (e.g. "must have the right to work in the UK," "unable to sponsor a visa" for a UK/EU/etc. role) — that is a location_fit problem (score location_fit weak), not a blocker. Blocker is specifically about US sponsorship for a US-based role.

SCORING FORMULA — start at 75, then adjust:
  role_fit:     strong +15 | moderate +5 | weak -20
  level_fit:    strong +10 | moderate +0 | weak -25
  location_fit: strong +5  | moderate +0 | weak -15
Cap at 100, floor at 0. A weak level_fit must land the role below 65.

If blocker is true, the role is unusable regardless of fit: set score to 0.
If the JD is empty/unavailable, score from title + location only, note that in reasoning, and discount role_fit by one tier.
"""


# The rubric + candidate profile are byte-identical on every call, so put them in
# a cached `system` block. On a warm cache (calls are <1s apart within a run) these
# ~1,100 tokens bill at 0.1x instead of full price on every one of ~1,300 calls.
# Caches are per-model, so both stages get their own warm cache. No beta header
# needed for basic ephemeral caching on the raw HTTP API.
SCORE_SYSTEM_BLOCKS = [{
    "type": "text",
    "text": SCORE_SYSTEM + "\n\nCANDIDATE PROFILE:\n" + CANDIDATE_PROFILE,
    "cache_control": {"type": "ephemeral"},
}]


def _call_model(model: str, title: str, location: str, raw_jd: str,
                role_family: Optional[str] = None) -> Optional[dict]:
    """One scoring call against `model`. Returns the guarded verdict dict, or None
    on any failure. The candidate profile lives in the cached system block, so the
    user message carries only the varying job."""
    prompt = (
        f"TITLE-SCREEN ROLE FAMILY GUESS: {role_family or 'unknown'}\n\n"
        f"JOB:\nTitle: {title}\nLocation: {location}\n\n"
        f"Description:\n{(raw_jd or '')[:JD_MAX_CHARS]}"
    )
    payload = {
        "model": model,
        "max_tokens": 1024,
        "system": SCORE_SYSTEM_BLOCKS,
        "messages": [{"role": "user", "content": prompt}],
    }
    # Sonnet 5 (and other 4.6+ models) run adaptive thinking by DEFAULT, which eats
    # max_tokens and truncates the JSON — disable it. Haiku 4.5 doesn't think by
    # default and rejects {"type":"disabled"}, so for it we leave thinking off by
    # omission rather than sending the (invalid) disable flag.
    if "haiku" not in model:
        payload["thinking"] = {"type": "disabled"}
    try:
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
            timeout=45,
        )
        r.raise_for_status()
        body = r.json()
        # Track cache usage per model so we can see if caching is actually firing.
        u = body.get("usage") or {}
        SCORE_STATS["cache_reads"][model]  = SCORE_STATS["cache_reads"].get(model, 0)  + int(u.get("cache_read_input_tokens") or 0)
        SCORE_STATS["cache_writes"][model] = SCORE_STATS["cache_writes"].get(model, 0) + int(u.get("cache_creation_input_tokens") or 0)
        SCORE_STATS["input_tokens"][model] = SCORE_STATS["input_tokens"].get(model, 0) + int(u.get("input_tokens") or 0)
        # Pick the text block explicitly rather than assuming content[0].
        blocks = body.get("content", [])
        text = next((b.get("text", "") for b in blocks if b.get("type") == "text"), "").strip()
        text = re.sub(r"^```json\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
        # Parse the FIRST JSON object and ignore anything after it. Haiku is
        # chattier than Sonnet and often appends a sentence of explanation after
        # the JSON despite "output only JSON" — plain json.loads() then throws
        # "Extra data", which was silently dropping ~14% of scores. raw_decode
        # reads one value and stops; skipping to the first "{" also tolerates any
        # leading prose.
        start = text.find("{")
        if start > 0:
            text = text[start:]
        res, _ = json.JSONDecoder().raw_decode(text)
        return _apply_score_guards(res)
    except Exception as e:
        log.warning(f"Score failed ({model}) for '{title}': {e}")
        return None


def score_job(title: str, location: str, raw_jd: str, role_family: Optional[str] = None) -> Optional[dict]:
    """Two-stage scorer. The cheap Haiku screen runs on every job; only jobs that
    clear STAGE1_PASS get the authoritative (pricier) Sonnet verdict — so the
    expensive model never spends tokens rejecting the ~97% that don't fit."""
    SCORE_STATS["stage1"] += 1
    screen = _call_model(SCORE_MODEL_STAGE1, title, location, raw_jd, role_family)
    if screen is None:
        return None
    if int(screen.get("score", 0)) >= STAGE1_PASS:
        SCORE_STATS["stage2"] += 1
        confirmed = _call_model(SCORE_MODEL_STAGE2, title, location, raw_jd, role_family)
        if confirmed is not None:
            return confirmed  # Sonnet is authoritative
        # Sonnet failed — keep the Haiku screen rather than dropping the job.
    return screen


def _apply_score_guards(res: dict) -> dict:
    """Deterministic caps so a good-sounding JD can't sneak past the real
    disqualifiers, independent of model wording. Also folds role family +
    years-required into the stored reasoning so the board shows them without
    a schema change."""
    try:
        score = int(res.get("score", 0))
    except (TypeError, ValueError):
        score = 0

    # Hard blocker (citizenship / clearance / green card / no-sponsorship): unusable.
    if res.get("blocker") is True:
        score = 0
    # Experience/level miss: keep it below the match threshold regardless of how
    # the model scored it (a weak level_fit or an explicit experience miss).
    elif res.get("level_fit") == "weak" or res.get("meets_experience") is False:
        score = min(score, 55)

    res["score"] = max(0, min(100, score))

    fam = res.get("role_family")
    yrs = res.get("years_required")
    tag = ""
    if fam:
        tag = f"[{fam}" + (f", {yrs}y req" if isinstance(yrs, int) else "") + "] "
    res["reasoning"] = (tag + (res.get("reasoning") or ""))[:240]
    return res


# ── Supabase helpers ───────────────────────────────────────────────────────────

def sb_get(path: str, params: dict = None) -> list[dict]:
    r = httpx.get(
        f"{SUPABASE_URL}/rest/v1/{path}",
        headers=HEADERS_SB,
        params=params,
        timeout=20,
    )
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
    else:
        log.info(f"  upserted {len(rows)} rows -> {table}")


def sb_patch(table: str, filters: dict, data: dict):
    r = httpx.patch(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=HEADERS_SB,
        params={k: f"eq.{v}" for k, v in filters.items()},
        json=data,
        timeout=20,
    )
    if r.status_code not in (200, 204):
        log.warning(f"PATCH {table} {filters}: {r.status_code} {r.text[:200]}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    _load_config()
    log.info("=== Watchlist pipeline starting ===")

    companies = sb_get("companies", {"active": "eq.true", "select": "*"})
    log.info(f"Loaded {len(companies)} active companies")

    # Load existing open jobs for closed-detection
    existing_jobs = sb_get("jobs", {"status": "eq.open", "select": "id,company_id,ats_job_id"})
    existing_map  = {(j["company_id"], j["ats_job_id"]): j["id"] for j in existing_jobs}

    # Build a set of company_ids that already have jobs in the DB
    # These get the 26h recency filter; new companies get all jobs on first run
    companies_with_jobs = {j["company_id"] for j in existing_jobs}

    seen_this_run: set[tuple] = set()
    _now = datetime.now(timezone.utc)
    cutoff           = _now - timedelta(hours=CUTOFF_HOURS)
    first_run_cutoff = _now - timedelta(days=FIRST_RUN_DAYS)

    # Process order: FIRST-RUN companies (never seen before) first, then the
    # known set in a fresh random order each run. Two wins:
    #  - New discoveries always get their back-catalog seeded, even when we hit
    #    the 120-min workflow timeout (previously the tail was starved).
    #  - Known companies rotate day to day so no single group monopolises the
    #    front and starves the rest.
    first_run_cos = [c for c in companies if c["id"] not in companies_with_jobs]
    known_cos     = [c for c in companies if c["id"] in companies_with_jobs]
    random.shuffle(first_run_cos)   # fair order among new companies too
    random.shuffle(known_cos)
    companies = first_run_cos + known_cos
    log.info(f"Order: {len(first_run_cos)} first-run companies queued first, then {len(known_cos)} known (shuffled)")

    total_fetched = total_kept = total_scored = total_errors = 0
    all_scores: list[int] = []
    companies_with_matches: list[str] = []

    for co in companies:
        co_id, co_name, ats_type, ats_slug = (
            co["id"], co["name"], co["ats_type"], co["ats_slug"]
        )

        fetcher = ATS_FETCHERS.get(ats_type)
        if not fetcher:
            log.warning(f"{co_name}: no fetcher for {ats_type} — skipping")
            total_errors += 1
            continue

        # First-time companies seed a 30-day back-catalog; companies already in
        # the DB get the tight ~24h incremental window.
        is_first_run = co_id not in companies_with_jobs
        active_cutoff = first_run_cutoff if is_first_run else cutoff

        postings = fetcher(ats_slug)
        total_fetched += len(postings)

        relevant = [
            p for p in postings
            if p.get("ats_job_id") and p.get("title")
            and should_keep(p["title"], p.get("location", ""), p.get("posted_at"), active_cutoff)
        ]
        total_kept += len(relevant)

        tag = " [FIRST RUN]" if is_first_run else ""
        log.info(f"{co_name}{tag}: {len(postings)} fetched -> {len(relevant)} kept")

        # Track all fetched job IDs for closed-detection regardless
        for p in postings:
            if p.get("ats_job_id"):
                seen_this_run.add((co_id, p["ats_job_id"]))

        if not relevant:
            time.sleep(0.5)
            continue

        # Hydrate the full JD for every kept role that doesn't already have one.
        # Ashby and Lever include the JD in their list response; Greenhouse and
        # Workday need a per-posting call. A real JD is what lets the scorer catch
        # experience requirements ("7+ years") instead of guessing from the title.
        for p in relevant:
            if p.get("raw_jd"):
                continue
            if ats_type == "greenhouse":
                p["raw_jd"] = fetch_greenhouse_jd(p["ats_job_id"], ats_slug)
                time.sleep(0.2)
            elif ats_type == "workday":
                p["raw_jd"] = fetch_workday_jd(ats_slug, p.get("_ext_path", ""))
                time.sleep(0.2)

        now = datetime.now(timezone.utc).isoformat()

        sb_upsert("jobs", [
            {
                "company_id":   co_id,
                "ats_job_id":   p["ats_job_id"],
                "title":        p["title"],
                "location":     p.get("location", ""),
                "url":          p["url"],
                "posted_at":    p.get("posted_at"),
                "last_seen_at": now,
                "status":       "open",
                "raw_jd":       (p.get("raw_jd") or "")[:10000],
            }
            for p in relevant
        ], "company_id,ats_job_id")

        # Score jobs that don't have a match yet
        co_jobs = sb_get("jobs", {
            "company_id": f"eq.{co_id}",
            "status":     "eq.open",
            "select":     "id,title,location,raw_jd",
        })
        if not co_jobs:
            time.sleep(0.5)
            continue

        job_ids_csv = ",".join(j["id"] for j in co_jobs)
        matched_ids = {
            m["job_id"]
            for m in sb_get("matches", {
                "job_id": f"in.({job_ids_csv})",
                "select": "job_id",
            })
        }

        to_score = [j for j in co_jobs if j["id"] not in matched_ids]

        match_rows = []
        co_match_count = 0
        for job in to_score:
            family = classify_role(job["title"])
            result = score_job(job["title"], job.get("location", ""), job.get("raw_jd", ""), family)
            if result and isinstance(result.get("score"), int):
                all_scores.append(result["score"])
                # Always persist the score, even sub-threshold. Otherwise `to_score`
                # (jobs without a match row) re-scores every reject every day
                # forever — the single biggest cost leak observed in production
                # (~40% of daily Haiku calls were zombie re-scores).
                # v_watchlist filters by score >= SCORE_THRESHOLD so the board
                # still only shows matches.
                match_rows.append({
                    "job_id":       job["id"],
                    "score":        result["score"],
                    "role_fit":     result.get("role_fit"),
                    "level_fit":    result.get("level_fit"),
                    "location_fit": result.get("location_fit"),
                    "reasoning":    result.get("reasoning", ""),
                    "scored_at":    now,
                })
                if result["score"] >= SCORE_THRESHOLD:
                    total_scored += 1
                    co_match_count += 1
                    log.info(f"    {result['score']}: {job['title']}")
            time.sleep(0.5)

        if to_score:
            log.info(f"  scored {len(to_score)} -> {co_match_count} matched (>= {SCORE_THRESHOLD})")

        if match_rows:
            sb_upsert("matches", match_rows, "job_id")
            companies_with_matches.append(co_name)

        time.sleep(1)

    # Mark jobs that disappeared from ATS as closed
    closed_count = 0
    for (co_id, ats_job_id), job_id in existing_map.items():
        if (co_id, ats_job_id) not in seen_this_run:
            sb_patch("jobs", {"id": job_id}, {"status": "closed"})
            closed_count += 1

    score_summary = f"avg_score={sum(all_scores)/len(all_scores):.0f}" if all_scores else "no jobs scored"
    log.info(
        f"=== Done. companies={len(companies)} fetched={total_fetched} kept={total_kept} "
        f"scored={total_scored} closed={closed_count} errors={total_errors} "
        f"haiku_calls={SCORE_STATS['stage1']} sonnet_calls={SCORE_STATS['stage2']} ({score_summary}) ==="
    )
    # Per-model cache diagnostics: read/write/uncached tokens, plus hit-rate.
    # A cache-hit-rate of 0 with nonzero writes = prefix is below the model's
    # minimum cacheable size and caching is silently off (see SCORE_STATS docstring).
    for m_name in sorted(set(SCORE_STATS["input_tokens"]) | set(SCORE_STATS["cache_reads"]) | set(SCORE_STATS["cache_writes"])):
        rd = SCORE_STATS["cache_reads"].get(m_name, 0)
        wr = SCORE_STATS["cache_writes"].get(m_name, 0)
        uc = SCORE_STATS["input_tokens"].get(m_name, 0)
        denom = rd + uc
        hit_pct = (100.0 * rd / denom) if denom else 0.0
        log.info(f"    cache[{m_name}]: read={rd} written={wr} uncached_input={uc} hit_rate={hit_pct:.1f}%")
    if companies_with_matches:
        log.info(f"New matches from: {', '.join(companies_with_matches)}")


# ── Utilities ──────────────────────────────────────────────────────────────────

def _strip_html(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


if __name__ == "__main__":
    main()
