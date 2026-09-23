#!/usr/bin/env python3
"""
filters.py
----------
Deterministic Gate 1 for the public PM job board: the cheap, no-network,
no-LLM screen that every posting passes through before anything expensive
happens. Shared by the ingestion pipeline and by company discovery so both
apply identical rules.

Three things this decides:
  1. classify_title(title)    -> TitleVerdict(decision, reason, seniority)
  2. classify_location(loc)   -> LocationVerdict(country, remote_region, in_scope, needs_resolution)
  3. should_ingest(title, loc) -> bool   (the combined gate)

Scope, per the product decision:
  * Core PM only -- Product Manager / Product Owner / Product Lead and their
    seniority variants. FDE, Solutions, PMM, TPM, Product Design, Product Ops
    and Product Analyst are explicitly out. The previous version treated FDE as
    a first-class family, which is why 43% of the old board was Solutions
    Architects and Forward Deployed Engineers.
  * Seven countries -- US, GB, IE, DE, NL, SE, DK -- as an explicit allowlist.
    The old filter kept everything that wasn't recognisably non-US, which is a
    different and much leakier rule.

Two design points worth not undoing:

  * Include patterns anchor on the PM *noun*, never on a seniority prefix.
    A `\\b(senior|staff|principal) product\\b` pattern looks reasonable and
    quietly admits "Senior Product Security Engineer", "Staff Product Security
    AI Engineer" and "Senior Product Development Analyst" -- all real titles
    observed live. Anchoring on the noun and treating seniority as a modifier
    removed ~6% of apparent PM roles as false positives.

  * A title that matches BOTH an include and a conflicting family returns
    "uncertain" rather than being resolved here. Gate 2 (the LLM pass) reads
    the responsibilities and decides. Guessing in a regex is how the FDE
    problem happened in the first place.

Director/VP/Head-of roles are *classified, tagged and kept out of the board*
rather than dropped: seniority is detectable from the title deterministically,
so tagging them costs nothing and turning on a leadership track later becomes
a view change instead of a re-extraction.

Run `python filters.py --selftest` to exercise this offline.
"""

import re
import unicodedata
from typing import NamedTuple

TAXONOMY_VERSION = "pm-v1-2026-09"


# ── Normalisation ────────────────────────────────────────────────────────────

_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")


def _norm(s: str) -> str:
    """Lowercase, fold unicode dashes/nbsp, collapse whitespace.

    Live data carries U+00A0 in at least one title ("Product\xa0Owner, Integrated
    Business Planning"), which defeats any \\b-anchored pattern if left alone.
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s).translate(_DASHES)
    return re.sub(r"\s+", " ", s.lower()).strip()


# ── Title: the PM include set ────────────────────────────────────────────────
# Anchored on the noun. Seniority prefixes are handled separately, below.

_INCLUDE_RE = re.compile(
    r"\bproduct (manager|managers|management|mgr|owner|owners|lead|leads)\b"
    r"|\b(manager|owner|lead|head|director|vp) of product\b"
    r"|\bproduct (manager|owner)[,/-]"
    r"|\bpm\s?(i{1,3}|[123])\b"
    r"|\bassociate pm\b|\bapm\b",
    re.I,
)

# Families that are NOT this board. Ordered roughly by how often they showed up
# as false positives on the old board (Solutions/FDE first -- 229 of 503 rows).
_EXCLUDE_PATTERNS: list[tuple[str, str]] = [
    ("solutions_or_fde", r"\bforward[ -]?deployed\b|\bsolutions? (engineer|architect|consultant|lead|manager|specialist)\b"
                         r"|\bsales engineer\b|\bpre[ -]?sales\b"
                         r"|\b(implementation|deployment|customer|integration|field) engineer\b"
                         r"|\bapplied (ai|ml|scientist|engineer)\b|\bprofessional services\b"),
    ("product_marketing", r"\bproduct marketing\b|\bpmm\b|\bgrowth marketing\b"),
    ("program_management", r"\b(technical )?program manager\b|\bproject manager\b|\btpm\b"
                           r"|\bdelivery manager\b|\bscrum master\b|\brelease manager\b"),
    ("design", r"\bproduct design(er|)\b|\b(ux|ui) (designer|researcher)\b|\bdesign manager\b"),
    ("engineering", r"\bproduct engineer\b|\bproduct security\b|\bsoftware engineer\b"
                    r"|\b(backend|frontend|full[ -]?stack|platform|data|security|qa|test"
                    r"|hardware|firmware|mechanical|electrical) engineer\b|\bsre\b"),
    ("analytics", r"\bproduct analyst\b|\bproduct development analyst\b|\bdata (analyst|scientist)\b"
                  r"|\bbusiness analyst\b|\banalytics manager\b"),
    ("operations", r"\bproduct operations\b|\bproduct ops\b|\bbizops\b|\brevops\b"),
    # \bcounsel\b rather than \bproduct counsel\b: live boards carry
    # "Sr. Counsel, Product", which the narrower form misses.
    ("legal_or_support", r"\bcounsel\b|\bproduct support\b|\bproduct specialist\b"
                         r"|\bcustomer success\b|\baccount (manager|executive)\b"),
    ("too_junior", r"\bintern\b|\binterns\b|\binternship\b|\bapprentice\b|\bapprenticeship\b"
                   r"|\bco[ -]?op\b|\bworking student\b|\bnew grad\b"),
]
_EXCLUDE_RE = [(reason, re.compile(p, re.I)) for reason, p in _EXCLUDE_PATTERNS]

# Conflicts: a family whose presence alongside a PM noun means the title is
# genuinely ambiguous ("Technical Product Manager / Solutions Architect"), not
# that it's a clear reject. These escalate to Gate 2 instead of being decided here.
_CONFLICT_REASONS = {"solutions_or_fde", "product_marketing", "program_management",
                     "design", "engineering", "analytics", "operations"}

# ── Seniority (deterministic, from title alone) ──────────────────────────────

_DIRECTOR_PLUS_RE = re.compile(
    r"\bdirector\b|\bvp\b|\bsvp\b|\bevp\b|\bavp\b|\bvice president\b"
    r"|\bhead of\b|\bchief\b|\bpresident\b|\bcpo\b",
    re.I,
)
_STAFF_RE = re.compile(r"\b(staff|principal|distinguished)\b|\bgroup product\b|\blead product\b", re.I)
_SENIOR_RE = re.compile(r"\b(senior|sr\.?)\b", re.I)
_ASSOCIATE_RE = re.compile(r"\b(associate|junior|jr\.?|apm)\b|\bpm\s?i\b|\bproduct manager i\b", re.I)


class TitleVerdict(NamedTuple):
    decision: str            # "include" | "exclude" | "uncertain"
    reason: str | None       # exclusion family, or None when included
    seniority: str | None    # associate | mid | senior | staff_principal | director_plus


def _seniority(t: str) -> str | None:
    if _DIRECTOR_PLUS_RE.search(t):
        return "director_plus"
    if _STAFF_RE.search(t):
        return "staff_principal"
    if _SENIOR_RE.search(t):
        return "senior"
    if _ASSOCIATE_RE.search(t):
        return "associate"
    return "mid"


def classify_title(title: str) -> TitleVerdict:
    """Gate 1. Cheap, deterministic, no network.

    "include"   -> a core PM title; safe to ingest and classify
    "exclude"   -> a different function, or a leadership role we tag but don't show
    "uncertain" -> product-adjacent but ambiguous; Gate 2 reads the JD and decides
    """
    t = _norm(title)
    if not t:
        return TitleVerdict("exclude", "empty_title", None)

    hits = [reason for reason, rx in _EXCLUDE_RE if rx.search(t)]
    included = bool(_INCLUDE_RE.search(t))
    seniority = _seniority(t)

    if included:
        # Junior/intern is a hard reject even for a real PM title.
        if "too_junior" in hits:
            return TitleVerdict("exclude", "too_junior", seniority)
        # Leadership: tagged, ingested, filtered out of the board by the view.
        if seniority == "director_plus":
            return TitleVerdict("exclude", "leadership", seniority)
        conflict = next((h for h in hits if h in _CONFLICT_REASONS), None)
        if conflict:
            return TitleVerdict("uncertain", conflict, seniority)
        return TitleVerdict("include", None, seniority)

    if hits:
        return TitleVerdict("exclude", hits[0], seniority)
    if re.search(r"\bproduct\b", t):
        # "VP, Product" / "Chief Product Officer" never match the `X of product`
        # include form, but they are still leadership and still get tagged.
        if seniority == "director_plus":
            return TitleVerdict("exclude", "leadership", seniority)
        # Product-adjacent but no clean PM noun -- let Gate 2 look at the JD.
        return TitleVerdict("uncertain", "product_adjacent", seniority)
    return TitleVerdict("exclude", "not_product", seniority)


# ── Location: seven-country allowlist ────────────────────────────────────────
# Keep-by-default was the old rule and it leaks: the non-US tail of live ATS
# data is dominated by India and LatAm. This is an explicit allowlist instead.

ALLOWED_COUNTRIES = ("US", "GB", "IE", "DE", "NL", "SE", "DK")

_COUNTRY_RE: list[tuple[str, re.Pattern]] = [
    ("US", re.compile(
        r"\b(usa|u\.s\.a?|united states)\b"
        # "Remote - US", "Remote, USA", "US Remote" -- separators vary by ATS.
        r"|\bremote\s*[-,/|]?\s*(us|usa)\b|\b(us|usa)\s*[-,/|]?\s*remote\b"
        r"|\busa-\b|,\s*(al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks|ky|la|me|md|ma|mi|mn|ms|mo"
        r"|mt|ne|nv|nh|nj|nm|ny|nc|nd|oh|ok|or|pa|ri|sc|sd|tn|tx|ut|vt|va|wa|wv|wi|wy|dc)\b"
        r"|\b(new york|nyc|san francisco|bay area|san jose|mountain view|palo alto|menlo park"
        r"|sunnyvale|seattle|bellevue|austin|boston|cambridge, ma|los angeles|culver city|chicago"
        r"|denver|atlanta|san diego|dallas|houston|miami|philadelphia|phoenix|portland|nashville"
        r"|raleigh|pittsburgh|irvine|minneapolis|detroit|charlotte|arlington, va|mclean|tysons)\b",
        re.I)),
    ("GB", re.compile(r"\b(united kingdom|great britain|england|scotland|wales|gbr)\b|\buk\b"
                      r"|\b(london|manchester|edinburgh|glasgow|bristol|leeds|birmingham, uk)\b", re.I)),
    ("IE", re.compile(r"\b(ireland|irl|dublin|cork, ireland|galway)\b", re.I)),
    ("DE", re.compile(r"\b(germany|deutschland|deu|berlin|munich|muenchen|münchen|hamburg"
                      r"|frankfurt|cologne|koln|köln|stuttgart|dusseldorf|düsseldorf)\b", re.I)),
    ("NL", re.compile(r"\b(netherlands|holland|nld|amsterdam|utrecht|rotterdam|eindhoven|the hague)\b", re.I)),
    ("SE", re.compile(r"\b(sweden|sverige|swe|stockholm|gothenburg|goteborg|göteborg|malmo|malmö)\b", re.I)),
    ("DK", re.compile(r"\b(denmark|danmark|dnk|copenhagen|kobenhavn|københavn|aarhus)\b", re.I)),
]

# Anything here that is NOT also an allowed country puts the posting out of scope.
_OUT_OF_SCOPE_RE = re.compile(
    r"\b("
    r"canada|toronto|vancouver|montreal|ottawa|ontario|quebec|british columbia"
    r"|mexico|guadalajara|monterrey|brazil|brasil|sao paulo|são paulo|rio de janeiro"
    r"|argentina|buenos aires|colombia|bogota|bogotá|chile|santiago|peru|lima"
    r"|uruguay|montevideo|costa rica|dominican republic|santo domingo|latam|latin america"
    r"|india|bangalore|bengaluru|hyderabad|mumbai|delhi|gurgaon|gurugram|pune|chennai|noida"
    r"|singapore|malaysia|kuala lumpur|indonesia|jakarta|philippines|manila|thailand|bangkok"
    r"|vietnam|hanoi|hong kong|china|beijing|shanghai|shenzhen|taiwan|taipei"
    r"|japan|tokyo|korea|seoul|australia|sydney|melbourne|brisbane|new zealand|auckland"
    r"|israel|tel aviv|turkey|istanbul|uae|dubai|abu dhabi|saudi|qatar|egypt|cairo"
    r"|nigeria|lagos|kenya|nairobi|south africa|cape town|johannesburg"
    r"|france|paris|spain|madrid|barcelona|portugal|lisbon|porto|italy|rome|milan"
    r"|poland|warsaw|krakow|kraków|czech|prague|romania|bucharest|bulgaria|sofia"
    r"|hungary|budapest|greece|athens|ukraine|kyiv|belgium|brussels|switzerland|zurich"
    r"|geneva|austria|vienna|norway|oslo|finland|helsinki|estonia|tallinn|lithuania|latvia"
    r"|apac|anz"
    r")\b",
    re.I,
)

_REMOTE_RE = re.compile(r"\bremote\b|\banywhere\b|\bdistributed\b|\bwork from home\b|\bwfh\b", re.I)
_EMEA_RE = re.compile(r"\bemea\b|\beurope\b|\beuropean\b|\beu\b", re.I)
# Workday collapses multi-city requisitions to a literal count string.
_MULTI_LOC_RE = re.compile(r"^\d+\s+locations?$", re.I)


class LocationVerdict(NamedTuple):
    country: str | None        # ISO-2 when identifiable
    remote_region: str | None  # us | emea | unspecified | None (not remote)
    in_scope: bool
    needs_resolution: bool     # "3 Locations" -- resolvable only via a detail fetch


def classify_location(location: str) -> LocationVerdict:
    """Map a raw ATS location string onto the seven-country allowlist."""
    raw = (location or "").strip()
    l = _norm(raw)

    if not l:
        return LocationVerdict(None, "unspecified", True, False)

    if _MULTI_LOC_RE.match(l):
        # In scope pending resolution -- discarding these would drop real roles,
        # and treating the string as a location would poison the location filter.
        return LocationVerdict(None, "unspecified", True, True)

    is_remote = bool(_REMOTE_RE.search(l))
    found = [c for c, rx in _COUNTRY_RE if rx.search(l)]
    blocked = bool(_OUT_OF_SCOPE_RE.search(l))

    if found:
        # A hybrid like "New York or London" keeps the first allowed country,
        # even when an out-of-scope city is also named.
        region = ("us" if found[0] == "US" else "emea") if is_remote else None
        return LocationVerdict(found[0], region, True, False)

    if blocked:
        return LocationVerdict(None, None, False, False)

    if _EMEA_RE.search(l):
        # EMEA/Europe-wide postings are reachable from the allowed EU countries.
        return LocationVerdict(None, "emea", True, False)

    if is_remote:
        return LocationVerdict(None, "unspecified", True, False)

    # Named somewhere we don't recognise at all -- out, rather than keep-by-default.
    return LocationVerdict(None, None, False, False)


# ── Board scope: which tab a requisition belongs on ──────────────────────────
# The board has two views, US and International. A requisition that genuinely
# spans both -- "London, Stockholm, New York" -- appears in both rather than
# being forced into one, because forcing it would hide the role from half the
# audience it was posted for.

INTL_COUNTRIES = ("GB", "IE", "DE", "NL", "SE", "DK")


class ScopeVerdict(NamedTuple):
    scope: str                      # "us" | "intl" | "both" | "unknown"
    countries: tuple[str, ...]      # every allowed country named, deduped
    in_scope: bool
    remote_region: str | None
    needs_resolution: bool          # at least one location was opaque


def resolve_scope(locations) -> ScopeVerdict:
    """Fold one or more raw location strings into a board scope.

    Accepts the full location list for a requisition -- Workday's
    `location` + `additionalLocations`, Ashby's `secondaryLocations`, Lever's
    `categories.allLocations`, Greenhouse's `offices` -- so a multi-city post
    is judged on all of them rather than on whichever one the ATS happened to
    print first.
    """
    if isinstance(locations, str):
        locations = [locations]
    # Greenhouse packs several offices into one string ("Bellevue, Washington;
    # Mountain View, California; ..."), so split before classifying -- otherwise
    # a multi-country post collapses to whichever country matched first.
    expanded: list[str] = []
    for raw in (locations or []):
        if not raw or not str(raw).strip():
            continue
        expanded.extend(part.strip() for part in re.split(r"[;|]|\s+/\s+", str(raw)) if part.strip())
    locations = expanded
    if not locations:
        return ScopeVerdict("unknown", (), True, "unspecified", False)

    countries: list[str] = []
    regions: set[str] = set()
    any_in_scope = False
    needs_resolution = False

    for loc in locations:
        v = classify_location(loc)
        needs_resolution |= v.needs_resolution
        if v.in_scope:
            any_in_scope = True
        if v.country and v.country not in countries:
            countries.append(v.country)
        if v.remote_region:
            regions.add(v.remote_region)

    has_us = "US" in countries
    has_intl = any(c in INTL_COUNTRIES for c in countries)

    if has_us and has_intl:
        scope = "both"
    elif has_us:
        scope = "us"
    elif has_intl:
        scope = "intl"
    elif "us" in regions:
        scope = "us"
    elif "emea" in regions:
        scope = "intl"
    else:
        # Remote with no stated region, or still-opaque. Surfacing in both tabs
        # beats guessing and hiding it from one of them.
        scope = "unknown"

    remote_region = ("us" if "us" in regions else
                     "emea" if "emea" in regions else
                     "unspecified" if regions else None)
    return ScopeVerdict(scope, tuple(countries), any_in_scope, remote_region, needs_resolution)


def should_ingest(title: str, locations) -> bool:
    """Combined Gate 1. `uncertain` titles are ingested so Gate 2 can judge them."""
    if classify_title(title).decision == "exclude":
        return False
    return resolve_scope(locations).in_scope


# ── Self-test ────────────────────────────────────────────────────────────────

def _selftest() -> int:
    # Titles drawn from live board/ATS data wherever possible -- the excludes in
    # particular are real false positives the previous filter let through.
    title_cases = [
        # core includes
        ("Product Manager", "include", None, "mid"),
        ("Senior Product Manager, AI Platform", "include", None, "senior"),
        ("Staff Product Manager", "include", None, "staff_principal"),
        ("Principal Product Manager", "include", None, "staff_principal"),
        ("Group Product Manager (Core)", "include", None, "staff_principal"),
        ("Associate Product Manager", "include", None, "associate"),
        ("Technical Product Manager", "include", None, "mid"),
        ("Product Manager, Engineering Tools", "include", None, "mid"),
        ("Product Owner", "include", None, "mid"),
        ("Product Owner, Integrated Business Planning", "include", None, "mid"),
        ("Product Lead", "include", None, "mid"),
        ("Sr. Product Manager, Data Platform", "include", None, "senior"),
        # leadership: tagged, kept off the board
        ("Director of Product", "exclude", "leadership", "director_plus"),
        ("VP, Product", "exclude", "leadership", "director_plus"),
        ("Head of Product", "exclude", "leadership", "director_plus"),
        # the old board's actual false positives
        ("Forward Deployed Engineer", "exclude", "solutions_or_fde", None),
        ("Sr. Forward Deployed Engineer (FDE) - Financial Services", "exclude", "solutions_or_fde", None),
        ("Senior AI Solutions Engineer", "exclude", "solutions_or_fde", None),
        ("Solutions Architect", "exclude", "solutions_or_fde", None),
        ("Applied AI Engineer, Enterprise Tech", "exclude", "solutions_or_fde", None),
        ("Customer Engineer", "exclude", "solutions_or_fde", None),
        ("Senior Customer Engineer, Majors, San Francisco", "exclude", "solutions_or_fde", None),
        # the seniority-prefix trap
        ("Senior Product Security Engineer", "exclude", "engineering", None),
        ("Staff Product Security AI Engineer", "exclude", "engineering", None),
        ("Senior Product Development Analyst (P&C Insurance)", "exclude", "analytics", None),
        # other families
        ("Product Marketing Manager", "exclude", "product_marketing", None),
        ("Technical Program Manager", "exclude", "program_management", None),
        ("Program Manager, Product", "exclude", "program_management", None),
        ("Product Designer", "exclude", "design", None),
        ("Product Analyst", "exclude", "analytics", None),
        # leadership wins over the function reason: we don't show director+ at all
        ("Director of Product Operations", "exclude", "leadership", "director_plus"),
        ("Software Engineer", "exclude", "engineering", None),
        ("Product Management Internship", "exclude", "too_junior", None),
        # early-career PM is a real audience; only interns/co-ops are too junior
        ("Junior Product Owner - Digital Operations", "include", None, "associate"),
        # ambiguous -> Gate 2
        ("Product Manager / Solutions Architect", "uncertain", "solutions_or_fde", "mid"),
        ("Product Builder, Performance Marketing Agents", "uncertain", "product_adjacent", "mid"),
    ]
    loc_cases = [
        # (location, country, in_scope, needs_resolution)
        ("", None, True, False),
        ("Remote", None, True, False),
        ("San Francisco, CA", "US", True, False),
        ("New York, NY (HQ)", "US", True, False),
        ("Remote - US", "US", True, False),
        ("Austin, Texas, United States of America", "US", True, False),
        ("Milwaukee, WI", "US", True, False),          # must not trip bare "uk"
        ("London", "GB", True, False),
        ("GBR-London", "GB", True, False),             # Workday ISO-3 form
        ("Amsterdam", "NL", True, False),
        ("Dublin, Ireland", "IE", True, False),
        ("Berlin", "DE", True, False),
        ("Stockholm", "SE", True, False),
        ("Copenhagen", "DK", True, False),
        ("New York or London", "US", True, False),     # hybrid keeps the US side
        ("Remote - EMEA", None, True, False),
        # explicitly out of scope
        ("Bengaluru, India", None, False, False),
        ("Toronto, Ontario, Canada", None, False, False),
        ("Buenos Aires, Argentina", None, False, False),
        ("Helsinki, Uusimaa, Finland", None, False, False),
        ("Singapore, Singapore", None, False, False),
        ("São Paulo", None, False, False),
        ("LatAm", None, False, False),
        ("Paris", None, False, False),
        # needs a detail fetch
        ("3 Locations", None, True, True),
    ]
    scope_cases = [
        # (locations, scope, in_scope)
        (["New York, NY"], "us", True),
        (["London"], "intl", True),
        (["London", "Stockholm"], "intl", True),
        (["London", "Stockholm", "New York, NY"], "both", True),
        (["Barcelona, Spain", "Lisbon, Portugal"], "unknown", False),
        (["Barcelona, Spain", "Austin, TX"], "us", True),       # one allowed city is enough
        (["Remote - US"], "us", True),
        (["Remote (EMEA)"], "intl", True),
        (["Remote"], "unknown", True),
        ([], "unknown", True),
        (["Bengaluru, India"], "unknown", False),
        ("San Francisco, CA", "us", True),                       # bare string accepted
        # Greenhouse packs multiple offices into one semicolon-joined string
        (["Bellevue, Washington; Mountain View, California"], "us", True),
        (["London; Berlin; New York, NY"], "both", True),
        # a genuinely cross-continent requisition belongs on both tabs
        (["Clearwater, Florida, United States", "Barcelona, Spain",
          "Bodegraven, Netherlands"], "both", True),
    ]

    failures = 0
    for title, want_d, want_r, want_s in title_cases:
        v = classify_title(title)
        ok = v.decision == want_d and v.reason == want_r
        if want_s is not None:
            ok = ok and v.seniority == want_s
        failures += not ok
        print(f"  [{'ok' if ok else 'FAIL'}] {title[:52]!r:<56} -> {v.decision}/{v.reason}/{v.seniority}"
              + ("" if ok else f"   WANT {want_d}/{want_r}" + (f"/{want_s}" if want_s else "")))
    for loc, want_c, want_scope, want_res in loc_cases:
        v = classify_location(loc)
        ok = v.country == want_c and v.in_scope == want_scope and v.needs_resolution == want_res
        failures += not ok
        print(f"  [{'ok' if ok else 'FAIL'}] {loc[:34]!r:<38} -> {v.country}/{v.remote_region}/"
              f"in_scope={v.in_scope}/resolve={v.needs_resolution}"
              + ("" if ok else f"   WANT {want_c}/{want_scope}/{want_res}"))

    for locs, want_scope, want_in in scope_cases:
        v = resolve_scope(locs)
        ok = v.scope == want_scope and v.in_scope == want_in
        failures += not ok
        label = locs if isinstance(locs, str) else ", ".join(locs) or "(none)"
        print(f"  [{'ok' if ok else 'FAIL'}] scope {label[:40]!r:<44} -> {v.scope}/"
              f"{'+'.join(v.countries) or '-'}/in_scope={v.in_scope}"
              + ("" if ok else f"   WANT {want_scope}/{want_in}"))

    total = len(title_cases) + len(loc_cases) + len(scope_cases)
    print(f"\n{total - failures}/{total} passed"
          + ("  ALL PASS" if not failures else f"  {failures} FAILURE(S)"))
    return 1 if failures else 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print("Usage: python filters.py --selftest")
