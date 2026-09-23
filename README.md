# Watchlist — public PM job board

Ingestion pipeline for a public Product Management job board. GitHub Actions
reads company ATS feeds, classifies each posting once, and writes structured
facts to Supabase. The [frontend](https://github.com/wishmur/watchlist-product-management)
reads views from that database.

**No visitor action ever triggers an LLM call.** That is structural, not a
policy: the frontend has no Anthropic key, no `supabase/functions/`, and no
server route that could reach one. The only API key lives in Actions secrets.

```
board.yml (6:30 AM PT daily)
  ├─ ingest.py      companies → ATS feeds → Gate 1 → jobs        [zero LLM calls]
  └─ classify.py    distinct postings → Gate 2 → job_facts        [capped spend]
                                                   └─ v_jobs_us / v_jobs_intl → board

expand.yml (6 AM PT daily)
  └─ expand_companies.py   discovers new companies from data/*.csv [zero LLM calls]
```

## Scope

- **Core PM only.** Product Manager, Product Owner, Product Lead and seniority
  variants. Forward-deployed and solutions engineering, program management,
  product marketing, design, analytics and product ops are excluded — every one
  of those titles contains a word that makes it look like a match.
- **Seven countries.** US, UK, Ireland, Germany, Netherlands, Sweden, Denmark,
  as an explicit allowlist. The board has a US tab and an International tab; a
  requisition genuinely open on both continents appears on both.
- **No fit score.** Roles are ranked by freshness and how completely the posting
  describes itself. Everything else is a filter.

## The two gates

**Gate 1 — `filters.py`.** Deterministic, no network, no model. Returns
`include` / `exclude` / `uncertain` per title, plus a country allowlist check.
Drops roughly half of everything before anything costs money.

`uncertain` matters: "Forward Deployed Product Manager" is a genuine coin-flip
between two families, and resolving cases like that in a regex is how 45% of the
previous board became Solutions Architects. Ambiguity escalates to Gate 2
instead of being guessed.

Include patterns anchor on the PM **noun**, never on a seniority prefix. A
plausible-looking `\b(senior|staff|principal) product\b` pattern silently admits
"Senior Product Security Engineer" and "Senior Product Development Analyst" —
both observed live, and together about 6% of apparent PM roles.

```bash
python3 scripts/filters.py --selftest     # 76 cases, offline
```

**Gate 2 — `classify.py`.** One Haiku call per distinct posting content, keyed
by `(company_id, content_hash)`, with a tool schema rather than "reply with
JSON". Cross-posted requisitions — one company posting the same req in 14 cities
— share a single extraction.

A tool-schema `enum` is a strong hint, not an enforced constraint; the model has
returned values outside it. Every enum-constrained field is clamped to the
vocabulary before insert, or one bad value fails the whole batch.

## Spend control

| Layer | Knob | Default |
|---|---|---|
| Hard call budget | `MAX_CLASSIFY_CALLS` | 1500 |
| Token-spend abort | `RUN_BUDGET_USD` | $3.00 |
| Accounting | `pipeline_runs` table | — |

Ingestion is free — it makes no model calls — so it runs wide. All spend is in
classification. An unpriced model raises rather than running uncapped.

`FIRST_RUN_DAYS` (60) is **not** the backfill window and the two should not be
conflated. A 7-day first-run window captures 0 of Databricks' 24 open PM roles
and 0 of Ramp's 6, because companies do not post in the week they are
discovered — and the next run treats them as known, losing the back-catalogue
permanently. The freshness window belongs on `classify.py --since-days`, which
is the only stage that costs anything.

## Accuracy

`run_eval.py` measures the PM / not-PM decision against `eval/golden_set.yaml`:
85 real postings, deliberately oversampling the FDE and solutions titles that
dominated the old board.

```
precision  100.0%   (0 false positives across 30 negatives)
recall     100.0%
85 examples, ~$0.41 per run
```

Precision is the headline. This board can afford to miss a role; it cannot
afford to tell someone a solutions architect job is product management.

```bash
python3 scripts/run_eval.py --dry-run     # cost estimate, no API calls
python3 scripts/run_eval.py --no-write    # run, print, persist nothing
```

## Setup

Run the SQL files in order in the Supabase SQL editor:

```
sql/001_initial_schema.sql
sql/002_rls_policies.sql
sql/005_public_read_hardening.sql   -- see the warning below
sql/008_pm_board_v1.sql             -- job_facts, board views, eval tables
```

`003`, `004`, `006` and `007` are superseded: `006` designed the job_facts layer
but was never applied, and `008` replaces it. Verify what is actually live:

```bash
python3 scripts/check_schema.py
```

> **Resolved 2026-09-23.** For a period this README claimed `sql/005` was
> applied when it was not, and the public anon key could read `companies`,
> `jobs` and `matches` directly — every score ever computed, including rejects
> whose `reasoning` text was candidate-specific, plus the full `raw_jd` of
> every posting. `005` and `008` are now applied and `check_schema.py` passes
> 19/19, including the assertion that anon is blocked from all three raw tables.
>
> To be clear about what the problem was: **not** that the publishable key is
> public. It is supposed to be — it ships in the frontend bundle by design and
> is gated by RLS. The problem was that RLS granted it more than it should have
> had. Fixing the policy fixed it; the key does not need rotating, and a new
> one would carry identical permissions. No secret key has ever been committed
> to either repo.

### Secrets

| Secret | Where |
|---|---|
| `SUPABASE_URL` | Supabase → Project Settings → API |
| `SUPABASE_SERVICE_KEY` | same page, service_role (not anon) |
| `ANTHROPIC_API_KEY` | console.anthropic.com |

`board.yml` uses the same three names as the existing workflows, so no new
repo secrets are needed.

Locally, copy `.env.example` to `.env` and fill it in — `scripts/local_env.py`
loads it automatically, so there is no need to `source` anything first. Real
environment variables always win over the file, which is how Actions supplies
these from secrets. `SUPABASE_ANON_KEY` is optional and is not a secret; set it
so `check_schema.py` can verify the public read surface from the outside.

## Running locally

```bash
pip install -r requirements.txt

python3 scripts/ingest.py --dry-run --limit 20        # fetch + screen, write nothing
python3 scripts/classify.py --dry-run                 # worklist + cost estimate
python3 scripts/classify.py --since-days 7            # launch backfill
python3 scripts/classify.py --restale                 # re-extract stale versions
```

### Order matters: ingest before classify

`v_jobs_public` requires `jobs.board_scope`, which only `ingest.py` sets. Rows
that predate it have it `NULL` and will not appear on the board no matter how
many of them are classified.

`content_hash` is the sharper trap. `sql/008` backfilled it from whatever
`raw_jd` happened to be stored at migration time, but `ingest.py` re-fetches
descriptions and recomputes the hash from the fresh text — Greenhouse in
particular now comes from a different endpoint. The two hashes do not match, so
`job_facts` rows written before an ingest are keyed to content that no longer
exists: orphaned, invisible, and paid for.

Classification is the only step that costs money, so getting this backwards is
the one sequencing mistake with a bill attached.

```bash
python3 scripts/ingest.py                  # free; populates board_scope + content_hash
python3 scripts/classify.py --since-days 7 # then spend
```

A full ingest across ~2,700 companies takes roughly two hours of HTTP. Run it
in slices with `--limit` if you would rather not hold a terminal open, or let
`board.yml` do it on schedule — it runs both stages in the right order.

## ATS notes

- **Greenhouse** — the per-job endpoint carries `offices`, `departments`,
  `content` and `first_published` in the call already being made. Use
  `first_published`, not `updated_at`: an edited old req looks new otherwise.
- **Ashby** — returns `{"jobs": [...]}` with `job.location`. It was once
  `jobPostings` / `locationName`, and reading the old key returns an empty list
  rather than raising, so Ashby failed **silently** for months — 21 seeded
  companies, 0 board rows, clean logs.
- **Lever** — `categories.allLocations` gives the full location list.
- **Workday** — `searchText` is load-bearing. With `""` it returns the head of
  the whole board unordered: 16.6% of postings crawled across 30 large tenants
  and 23 PM roles, versus 77 for two targeted queries. One query is not enough
  for this scope — `"product manager"` alone recalls 95.1%, adding
  `"product owner"` reaches 98.8%, `"product lead"` adds nothing. Opaque
  `"3 Locations"` strings are resolved via the detail endpoint.

Coverage is not uniform: Greenhouse, Ashby and Lever carry most European tech
employers; Workday adds volume but skews to US retail and industrial tenants.
Several European employers (Klarna, Wise, Revolut, Booking, Miro) are on
platforms this pipeline does not support at all.

## Files

```
.github/workflows/
  board.yml              ingest + classify (new pipeline)
  main.yml               legacy fetch+score — still the scheduled job
  expand.yml             company discovery
scripts/
  ats.py                 four ATS platforms behind one shape
  filters.py             Gate 1 — title + country, deterministic
  ingest.py              fan-out, dedupe, closed-detection   [no LLM]
  classify.py            Gate 2 — one Haiku call per posting [capped]
  taxonomy.py            controlled vocabulary, versioned
  run_eval.py            precision/recall on the golden set
  check_schema.py        asserts the live schema matches the code
  expand_companies.py    company discovery                   [no LLM]
sql/008_pm_board_v1.sql  job_facts, board views, eval tables
eval/golden_set.yaml     85 labelled real postings
data/*.csv               ~12,500 company slugs across 4 platforms
```
