# Watchlist Pipeline

GitHub Actions cron job that fetches open Product Manager and Forward-Deployed / Applied-AI Engineer roles from ATS APIs, scores them against a candidate profile via the Claude API, and upserts results into Supabase. The Lovable frontend at [watchlist-product-management.lovable.app](https://watchlist-product-management.lovable.app) reads from Supabase and displays the live board.

**Architecture:**

```
GitHub Actions (7 AM PT daily)
  └─ fetch_and_score.py
       ├─ pulls companies from Supabase
       ├─ fetches postings from Greenhouse / Ashby / Lever / Workday
       ├─ keeps PM + FDE titles in any US location (filters.py)
       ├─ fetches the JD for each kept role
       ├─ scores each new job two-stage: Haiku screens → Sonnet confirms the top ones
       └─ upserts jobs + matches into Supabase
            └─ Lovable reads v_watchlist view → public board

expand_companies.py (run manually, whenever you want to widen the list)
  ├─ reads data/{greenhouse,ashby,lever,workday}.csv (~12.5k companies, bundled locally)
  ├─ skips anything already in Supabase
  ├─ live-checks each remaining company's job board for an open PM or FDE role
  │  (no Claude calls — pure HTTP checks, free)
  └─ writes new_companies.sql for anything with a live match today
```

---

## Setup (one time)

### 1. Supabase

Run these SQL files in order in your Supabase SQL Editor:

```
sql/001_initial_schema.sql   — tables (companies, jobs, matches) + v_watchlist view
sql/002_rls_policies.sql     — public read, no anon writes
sql/seed.sql                 — initial company list
```

The board is read-only — there's no write path or password to configure.

> Upgrading an older database that still has the application-tracking tables?
> Run `sql/003_remove_applications.sql` once to drop them and rebuild the view.
> A fresh install from the files above never creates them, so you can skip it.
>
> Also run `sql/004_filter_v_watchlist_by_score.sql` once — required so the
> board keeps showing only ≥65 matches now that `fetch_and_score.py` stores
> every score (the fix that stopped daily re-scoring of rejects).
>
> Also run `sql/005_public_read_hardening.sql` once — required for the
> frontend's header stats (companies tracked, ATS platforms covered) and
> market-snapshot section. It replaces the frontend's direct reads of the
> raw `companies`/`matches` tables with two narrow aggregate views
> (`v_watchlist_meta`, `v_ats_coverage`) and revokes public SELECT on the
> raw tables, which previously let anyone with the (necessarily public)
> anon key query every score ever computed, not just the curated board.

### 2. GitHub repo

Create a new private repo (or use this one). Push this folder — including `data/`, it's ~830KB of CSVs and needed for `expand_companies.py`.

### 3. GitHub Secrets

Go to **Settings → Secrets → Actions** and add:

| Secret name           | Where to find it |
|-----------------------|-----------------|
| `SUPABASE_URL`        | Supabase → Project Settings → API → Project URL |
| `SUPABASE_SERVICE_KEY`| Supabase → Project Settings → API → service_role key (not anon) |
| `ANTHROPIC_API_KEY`   | console.anthropic.com → API Keys |

### 4. Trigger a manual run

Go to **Actions → Watchlist — daily job fetch + score → Run workflow**.

Set **dry_run = true** first to validate the fetch without writing to DB. Check the logs. If companies and job counts look right, run again with dry_run = false.

### 5. Verify in Supabase

```sql
-- Check what got populated
select count(*) from jobs;
select count(*) from matches;
select * from v_watchlist order by score desc limit 10;
```

---

## Widening the company list

`scripts/expand_companies.py` reads a bundled local dataset (`data/*.csv`, ~12,500 companies across Greenhouse, Ashby, Lever, and Workday) — no external dependency, no cost, and about 6x the coverage of the old `--limit 2000` default.

This runs automatically, **daily at 6 AM PT**, via **`.github/workflows/expand.yml`** — one hour before the fetch+score run — so newly found companies are scored the same morning. It writes new companies straight into Supabase (`tier=explore`, `source=discovered`, `active=true`). A company only gets written if it currently has a live posting matching your PM/FDE + US-location filters — nothing gets added on name alone.

Every run still produces `new_companies.sql` as an audit log — visible in the GitHub Actions run summary and attached as a downloadable artifact — purely so you can see what got added and why, or clean up a bad match later with a one-line `update companies set active = false where ...`. It's a paper trail, not a gate.

### Trigger it manually / adjust scope

Go to **Actions → Watchlist — discover new companies → Run workflow** any time you don't want to wait for the 6 AM run, or to scan a narrower slice:

- `ats`: which platforms to check (default: all 4)
- `limit`: cap candidates per ATS (default: 0 = full list)
- `tier`: what tier to tag new companies with (default: explore)
- `dry_run`: check this to skip the Supabase write and only produce the audit-log SQL for a one-off manual review

Expect a full 4-ATS scan (~12.5k companies) to take roughly 10-20 minutes — it's a lot of small HTTP requests, not an expensive operation, and it makes zero Claude calls either way.

### Or run it locally

```bash
export SUPABASE_URL=https://xxxx.supabase.co
export SUPABASE_SERVICE_KEY=your-service-role-key
pip install httpx

python scripts/expand_companies.py                      # writes straight to Supabase, all 4 ATS types
python scripts/expand_companies.py --dry-run             # SQL file only, nothing written
python scripts/expand_companies.py --ats workday         # just one ATS type
```

---

## Updating companies

Edit `sql/seed.sql` and re-run it in Supabase SQL Editor. The `ON CONFLICT` clause makes it idempotent — existing rows update, new ones insert. To disable a company without deleting it:

```sql
update companies set active = false where name = 'Acme Corp';
```

To add a new company inline (without editing seed.sql):

```sql
insert into companies (name, ats_type, ats_slug, tier, active, source, notes)
values ('Retool', 'greenhouse', 'retool', 'strong', true, 'manual', null)
on conflict (ats_type, ats_slug) do nothing;
```

### Finding ATS slugs

- **Greenhouse**: visit `boards.greenhouse.io/{slug}` — the slug is in the URL on their careers page
- **Ashby**: visit `jobs.ashbyhq.com/{slug}` — same pattern
- **Lever**: visit `jobs.lever.co/{slug}`
- **Workday**: visit their careers page, look at the URL: `{tenant}.wd{N}.myworkdayjobs.com/{site}` → slug = `wd{N}/{tenant}/{site}`
- Or just check `data/{ats}.csv` — it's a lot faster than hunting on the live site, and it's what `expand_companies.py` uses.

---

## Filtering & scoring

Two stages: a cheap deterministic **filter** (no Claude), then an LLM **score** on the survivors.

**Filters** live in `scripts/filters.py` — the single source of truth shared by `fetch_and_score.py` and `expand_companies.py` (they used to be copy-pasted and drifted). Two decisions:

- `classify_role(title)` → `"pm" | "fde" | None`. Two target families: **PM** (Product Manager / Product Lead, any IC seniority) and **FDE** (Forward Deployed / Applied AI / Solutions / Deployment / Implementation Engineer). Only exec/people-manager titles (Director, VP, Head-of, Chief) and interns are hard-dropped; Staff / Principal / Lead / Group PM pass through and are judged by the scorer.
- `is_us_location(location)` → keeps **all US locations + remote/unspecified**; drops only clearly non-US postings. (No metro allow-list — a role in Denver or Atlanta is kept, not silently dropped.)

Run the offline check for both:
```bash
python scripts/filters.py --selftest
```

**Scoring is two-stage, to keep token cost down.** The candidate profile is in `profile/candidate_profile.md` — update it as your experience changes. Because ~97% of scanned jobs get rejected, a cheap model does the screening and the pricey one only judges the promising few:

- **Stage 1 — Claude Haiku** (`SCORE_MODEL_STAGE1`) scores *every* kept job.
- **Stage 2 — Claude Sonnet** (`SCORE_MODEL_STAGE2`) re-scores *only* jobs whose Haiku score ≥ `STAGE1_PASS` (default 55) — the authoritative verdict. Sub-55 jobs keep the Haiku score (below the 65 match threshold anyway) and never touch Sonnet.

Both stages use the same rubric, which:

- picks the right lens per role family (product-ownership fit for PM, ships-next-to-the-customer fit for FDE),
- applies the profile's **years-of-experience rule** (roles accepting "PM *or equivalent / adjacent / technical* experience" score well; rigid "5+/7+ years of pure PM, no adjacency" roles get a weak `level_fit` and are capped below threshold in code),
- treats **any US or remote location as strong** (location never drags a US role's score), and
- flags hard **blockers** (US citizenship / clearance / green card / no-sponsorship-ever) and forces those to 0.

Deterministic caps in `_apply_score_guards()` enforce the blocker/experience rules regardless of model wording, and fold the detected role family + years-required into the stored `reasoning` (so the board shows them without a schema change).

**Cost knobs** (all env-overridable): the profile + rubric ride in a **cached** `system` block (repeat calls bill those ~1,100 tokens at 0.1×); `JD_MAX_CHARS` (default 3000) caps how much of each JD is sent; `FIRST_RUN_DAYS` (default 14) bounds the back-catalog a brand-new company seeds. The final log line reports `haiku_calls` / `sonnet_calls` so you can see the split. Together these turn a full reset from ~$20 into a few dollars, and incremental daily runs into cents (only genuinely new postings are ever scored; existing matches are never re-scored).

`SCORE_THRESHOLD` (default 65) is the minimum score stored as a match. Jobs below it are still stored in `jobs` but won't appear in `v_watchlist` (which requires a match row). Raise to 70 for a cleaner board; lower to 60 to see more.

---

## Workday

Workday has no public API. The pipeline uses an undocumented JSON endpoint (`/wday/cxs/{tenant}/{site}/jobs`) that powers their own career-page search widget. Some tenants may still block or rate-limit it. Slugs in `data/workday.csv` were parsed directly from each tenant's live careers URL, so they should be accurate — but if a Workday company shows 0 jobs and you know they're hiring, double check the tenant/site against their current careers page; Workday tenants occasionally rename sites.

Not every big employer is on Workday, Greenhouse, Ashby, or Lever — Garmin, for instance, runs on iCIMS, and Hugging Face is on Workable. Both are left `active = false` in `seed.sql` with a note. Adding support for more ATS platforms is a bigger lift (a new fetcher function + parser per platform) and isn't included in this pass — flag it if it becomes worth the effort.

---

## Cron schedule

Two daily workflows, staggered so discovery finishes before scoring starts:

| Workflow | Time (Pacific) | Cron (UTC) | What it does |
|----------|----------------|------------|--------------|
| `.github/workflows/expand.yml` | 6 AM PT | `0 13 * * *` | discover new companies with live PM/FDE roles → Supabase |
| `.github/workflows/main.yml`   | 7 AM PT | `0 14 * * *` | fetch + score new postings → Supabase |

Both leave results in Supabase before ~8 AM. GitHub Actions crons are fixed UTC (no DST), so in winter (PST) they run an hour earlier — still before 8 AM. Crons can also drift up to ~15 min under load. To change a time, edit the `cron:` line in that workflow.

**Recency windows** (`fetch_and_score.py`): a company already in the DB only fetches its recent postings (`CUTOFF_HOURS`, ~last 24h); a brand-new company seeds a 30-day back-catalog on its first run (`FIRST_RUN_DAYS`). Postings with no date (all Workday) can't be dated, so they're always kept.

---

## File structure

```
.github/
  workflows/
    expand.yml           — daily cron 6 AM PT: company discovery (writes to Supabase)
    main.yml             — daily cron 7 AM PT: fetch + score (writes to Supabase)
data/
  greenhouse.csv          — ~4,970 companies
  ashby.csv                — ~2,860 companies
  lever.csv                 — ~2,110 companies
  workday.csv                — ~2,600 companies
scripts/
  fetch_and_score.py   — main daily pipeline
  expand_companies.py  — manual company-list widener
  filters.py           — shared PM/FDE title + US-location filters (run --selftest)
sql/
  001_initial_schema.sql
  002_rls_policies.sql
  003_remove_applications.sql   — one-time migration for older DBs (drops apply feature)
  004_filter_v_watchlist_by_score.sql   — one-time migration: filter board to score >= 65
  005_public_read_hardening.sql   — one-time migration: aggregate-only public views, revoke raw-table reads
  seed.sql
requirements.txt
README.md
```
