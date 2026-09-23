#!/usr/bin/env python3
"""
ats.py
------
The ATS fan-out layer: Greenhouse, Ashby, Lever and Workday behind one shape.
No LLM calls, no Supabase, no scoring — just "what is on this company's board".

Two phases, because the cheap one is enough to run Gate 1:

  list_postings(ats, slug) -> FetchResult   title + locations, one request
  hydrate(ats, slug, posting) -> Posting    JD + structured fields, per survivor

Gate 1 drops roughly half of everything, so hydrating only survivors keeps the
request count at 1 + N_kept per company instead of 1 + N_total. Ashby and Lever
return everything in the list call, so hydrate is a no-op there.

Failure is not emptiness
------------------------
Every fetch returns FetchResult with an explicit `ok` flag. The previous
implementation swallowed exceptions to `[]` with a log warning, and the caller
then treated "no postings seen" as "every posting closed" — so one transient
500 from Greenhouse would close a company's entire board. Callers must check
`ok` before running closed-detection. That is the whole reason this type exists.

Structured before inferred
--------------------------
Every field an ATS states outright is read here rather than guessed by a model
downstream: department, team, employment type, workplace type, remote flag,
compensation where published, and — importantly — the *full* location list.
A requisition cross-posted to London and Stockholm carries both, so the board
can place it correctly instead of showing whichever city the ATS printed first.
"""

from __future__ import annotations

import html
import logging
import os
import re
import time
from typing import Any, Callable, NamedTuple, Optional

import httpx

log = logging.getLogger("ats")

HTTP_TIMEOUT = float(os.getenv("ATS_HTTP_TIMEOUT", "20"))
HTTP_RETRIES = int(os.getenv("ATS_HTTP_RETRIES", "3"))
USER_AGENT = os.getenv("ATS_USER_AGENT", "watchlist-pm-board/1.0")


class Posting(dict):
    """A single posting. dict so it serialises trivially; keys are stable."""


class FetchResult(NamedTuple):
    ok: bool
    postings: list[Posting]
    error: Optional[str] = None


# ── HTTP with retry/backoff ──────────────────────────────────────────────────

_RETRYABLE = {408, 425, 429, 500, 502, 503, 504}


def _request(method: str, url: str, **kw) -> httpx.Response:
    """One request with bounded exponential backoff.

    Raises on final failure so the caller can report ok=False rather than
    silently returning an empty board.
    """
    headers = {"user-agent": USER_AGENT, **kw.pop("headers", {})}
    last: Exception | None = None
    for attempt in range(HTTP_RETRIES):
        try:
            r = httpx.request(method, url, headers=headers, timeout=HTTP_TIMEOUT, **kw)
            if r.status_code in _RETRYABLE and attempt < HTTP_RETRIES - 1:
                delay = 2 ** attempt
                retry_after = r.headers.get("retry-after")
                if retry_after and retry_after.isdigit():
                    delay = min(int(retry_after), 30)
                log.debug(f"{url} -> {r.status_code}, retrying in {delay}s")
                time.sleep(delay)
                continue
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001 - re-raised below
            last = e
            if attempt < HTTP_RETRIES - 1:
                time.sleep(2 ** attempt)
    raise last  # type: ignore[misc]


def _get(url: str, **kw) -> httpx.Response:
    return _request("GET", url, **kw)


def _post(url: str, **kw) -> httpx.Response:
    return _request("POST", url, **kw)


# ── Helpers ──────────────────────────────────────────────────────────────────

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\n{3,}")


def strip_html(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"<(br|/p|/div|/li|/h[1-6])\s*/?>", "\n", s, flags=re.I)
    s = _TAG_RE.sub("", s)
    return _WS_RE.sub("\n\n", html.unescape(s)).strip()


def _clean(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _blank() -> Posting:
    return Posting(
        ats_job_id="", title="", locations=[], url="", posted_at=None, raw_jd="",
        department=None, team=None, employment_type=None, workplace_type=None,
        is_remote=None, comp_min=None, comp_max=None, comp_currency=None,
        _detail=None,
    )


# ── Greenhouse ───────────────────────────────────────────────────────────────

def list_greenhouse(slug: str) -> FetchResult:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    try:
        jobs = _get(url).json().get("jobs", [])
    except Exception as e:
        return FetchResult(False, [], f"greenhouse/{slug}: {e}")
    out = []
    for j in jobs:
        p = _blank()
        p.update(
            ats_job_id=str(j.get("id", "")),
            title=j.get("title", "") or "",
            locations=[n for n in [(j.get("location") or {}).get("name")] if n],
            url=j.get("absolute_url", "") or "",
            # updated_at makes an edited old req look new; first_published is the
            # real posting date and the per-job payload carries it.
            posted_at=_clean(j.get("first_published")) or _clean(j.get("updated_at")),
            _detail=str(j.get("id", "")),
        )
        out.append(p)
    return FetchResult(True, out)


def hydrate_greenhouse(slug: str, p: Posting) -> Posting:
    """One call adds offices, departments, first_published and the JD."""
    if not p.get("_detail"):
        return p
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{p['_detail']}"
    try:
        j = _get(url).json()
    except Exception as e:
        log.debug(f"greenhouse detail {slug}/{p['_detail']}: {e}")
        return p
    offices = [o.get("name") for o in (j.get("offices") or []) if o.get("name")]
    depts = [d.get("name") for d in (j.get("departments") or []) if d.get("name")]
    locs = list(dict.fromkeys([*p["locations"], *offices]))
    p.update(
        locations=locs or p["locations"],
        raw_jd=strip_html(j.get("content", "")),
        department=depts[0] if depts else None,
        posted_at=_clean(j.get("first_published")) or p.get("posted_at"),
        is_remote=any("remote" in (o or "").lower() for o in offices) or None,
    )
    return p


# ── Ashby ────────────────────────────────────────────────────────────────────

def list_ashby(slug: str) -> FetchResult:
    """Ashby returns everything in one call, including secondary locations.

    Note the response key is `jobs` with `job.location` — it was once
    `jobPostings` with `job.locationName`, and reading the old key returned an
    empty list rather than raising, so Ashby failed silently for months.
    """
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    try:
        jobs = _get(url).json().get("jobs", [])
    except Exception as e:
        return FetchResult(False, [], f"ashby/{slug}: {e}")
    out = []
    for j in jobs:
        if j.get("isListed") is False:
            continue
        secondary = [
            s.get("location") for s in (j.get("secondaryLocations") or [])
            if s.get("location")
        ]
        comp = j.get("compensation") or {}
        summary = comp.get("summaryComponents") or []
        lo = hi = cur = None
        for c in summary:
            if c.get("compensationType") == "Salary":
                lo, hi = c.get("minValue"), c.get("maxValue")
                cur = c.get("currencyCode")
                break
        p = _blank()
        p.update(
            ats_job_id=str(j.get("id", "")),
            title=j.get("title", "") or "",
            locations=[l for l in [j.get("location"), *secondary] if l],
            url=j.get("jobUrl", "") or "",
            posted_at=_clean(j.get("publishedAt")),
            raw_jd=j.get("descriptionPlain") or strip_html(j.get("descriptionHtml", "")),
            department=_clean(j.get("department")),
            team=_clean(j.get("team")),
            employment_type=_clean(j.get("employmentType")),
            workplace_type=_clean(j.get("workplaceType")),
            is_remote=j.get("isRemote"),
            comp_min=lo, comp_max=hi, comp_currency=cur,
        )
        out.append(p)
    return FetchResult(True, out)


# ── Lever ────────────────────────────────────────────────────────────────────

def list_lever(slug: str) -> FetchResult:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        jobs = _get(url).json()
        if not isinstance(jobs, list):
            return FetchResult(False, [], f"lever/{slug}: unexpected payload")
    except Exception as e:
        return FetchResult(False, [], f"lever/{slug}: {e}")
    out = []
    for j in jobs:
        cats = j.get("categories") or {}
        all_locs = cats.get("allLocations") or []
        created = j.get("createdAt")
        posted = None
        if isinstance(created, (int, float)):
            posted = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(created / 1000))
        p = _blank()
        p.update(
            ats_job_id=str(j.get("id", "")),
            title=j.get("text", "") or "",
            locations=list(dict.fromkeys([l for l in [cats.get("location"), *all_locs] if l])),
            url=j.get("hostedUrl", "") or "",
            posted_at=posted,
            raw_jd=j.get("descriptionPlain") or strip_html(j.get("description", "")),
            department=_clean(cats.get("department")),
            team=_clean(cats.get("team")),
            employment_type=_clean(cats.get("commitment")),
            workplace_type=_clean(j.get("workplaceType")),
        )
        out.append(p)
    return FetchResult(True, out)


# ── Workday ──────────────────────────────────────────────────────────────────
# searchText is load-bearing. With "" Workday returns the head of the whole
# board unordered: 16.6% of postings crawled across 30 large tenants and 23 PM
# roles, versus 77 for the two-query form. "product manager" alone recalls
# 95.1% of PM titles; adding "product owner" reaches 98.8%; "product lead" adds
# nothing on top of that pair.

WORKDAY_QUERIES = ["product manager", "product owner"]
WORKDAY_PAGES = int(os.getenv("WORKDAY_PAGES", "3"))


def _workday_parts(slug: str) -> Optional[tuple[str, str, str]]:
    try:
        wd_host, tenant, site = slug.split("/", 2)
        return wd_host, tenant, site
    except ValueError:
        return None


def list_workday(slug: str) -> FetchResult:
    parts = _workday_parts(slug)
    if not parts:
        return FetchResult(False, [], f"workday/{slug}: malformed slug (want wdN/tenant/site)")
    wd_host, tenant, site = parts
    base = f"https://{tenant}.{wd_host}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{tenant}/{site}/jobs"

    queries = list(WORKDAY_QUERIES)
    if os.getenv("WORKDAY_DEEP_SEARCH", "").lower() in ("1", "true", "yes"):
        queries.append("product")

    out: list[Posting] = []
    seen: set[str] = set()
    try:
        for query in queries:
            offset = 0
            for _ in range(WORKDAY_PAGES):
                data = _post(
                    api,
                    json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": query},
                    headers={"content-type": "application/json"},
                ).json()
                batch = data.get("jobPostings", [])
                if not batch:
                    break
                for j in batch:
                    ext = j.get("externalPath", "") or ""
                    if ext and ext in seen:
                        continue
                    seen.add(ext)
                    p = _blank()
                    p.update(
                        ats_job_id=(ext.rsplit("_", 1)[-1] if ext else "") or j.get("title", ""),
                        title=j.get("title", "") or "",
                        # Often the literal "3 Locations"; hydrate resolves it.
                        locations=[l for l in [j.get("locationsText")] if l],
                        url=f"{base}/{site}{ext}" if ext else base,
                        posted_at=None,
                        _detail=ext,
                    )
                    out.append(p)
                offset += 20
                if offset >= data.get("total", 0):
                    break
                time.sleep(0.15)
    except Exception as e:
        return FetchResult(False, [], f"workday/{slug}: {e}")
    return FetchResult(True, out)


def hydrate_workday(slug: str, p: Posting) -> Posting:
    """Resolves "N Locations" into real cities, and supplies the JD.

    The detail payload carries `location` plus `additionalLocations`, which is
    the only way to turn Workday's opaque multi-location string into something
    the board can filter on.
    """
    parts = _workday_parts(slug)
    if not parts or not p.get("_detail"):
        return p
    wd_host, tenant, site = parts
    url = f"https://{tenant}.{wd_host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{p['_detail']}"
    try:
        info = _get(url).json().get("jobPostingInfo", {})
    except Exception as e:
        log.debug(f"workday detail {slug}: {e}")
        return p
    locs = [l for l in [info.get("location"), *(info.get("additionalLocations") or [])] if l]
    p.update(
        locations=list(dict.fromkeys(locs)) or p["locations"],
        raw_jd=strip_html(info.get("jobDescription", "")),
        employment_type=_clean(info.get("timeType")),
        # Workday exposes only relative strings ("Posted 30+ Days Ago"); startDate
        # is the nearest thing to a real timestamp it gives.
        posted_at=_clean(info.get("startDate")) or p.get("posted_at"),
    )
    return p


# ── Registry ─────────────────────────────────────────────────────────────────

LISTERS: dict[str, Callable[[str], FetchResult]] = {
    "greenhouse": list_greenhouse,
    "ashby": list_ashby,
    "lever": list_lever,
    "workday": list_workday,
}

HYDRATORS: dict[str, Callable[[str, Posting], Posting]] = {
    "greenhouse": hydrate_greenhouse,
    "workday": hydrate_workday,
    # ashby and lever return full detail in the list call.
}

SUPPORTED = tuple(LISTERS)


def list_postings(ats: str, slug: str) -> FetchResult:
    lister = LISTERS.get(ats)
    if not lister:
        return FetchResult(False, [], f"unsupported ats: {ats}")
    return lister(slug)


def hydrate(ats: str, slug: str, posting: Posting) -> Posting:
    hydrator = HYDRATORS.get(ats)
    return hydrator(slug, posting) if hydrator else posting


def needs_hydration(ats: str) -> bool:
    return ats in HYDRATORS
