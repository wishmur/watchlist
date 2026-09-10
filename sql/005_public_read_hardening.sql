-- 005_public_read_hardening.sql
-- Tightens public (anon) reads to exactly what the frontend needs.
--
-- Context: 002_rls_policies.sql granted blanket `using (true)` SELECT on the
-- raw companies/jobs/matches tables to anon/authenticated. The frontend's
-- anon key is necessarily public (embedded client-side), so that policy
-- meant anyone could bypass the app entirely and query the REST endpoint
-- directly for every score ever computed -- including sub-threshold and
-- blocker-zeroed rejects, whose `reasoning` text can reveal candidate-
-- specific screening criteria (visa/clearance blockers, experience-bar
-- reasoning) at a granularity the public board was never meant to expose.
-- v_watchlist already curates that down to score >= 65; the raw tables
-- undid the curation for anyone who looked.
--
-- In practice the frontend only ever read two things directly from the raw
-- tables: a count of active companies and the most recent scored_at, both
-- for the header stats. Those numbers carry no row-level data, so they move
-- to a small aggregate view instead of a raw-table grant.
--
-- fetch_and_score.py / expand_companies.py are unaffected: they write (and
-- read) with the service_role key, which bypasses RLS entirely.
--
-- Idempotent: safe to rerun. Run once in the Supabase SQL editor.

-- ============================================================
-- Aggregate-only public view: companies tracked, ATS platforms covered,
-- and last pipeline run. No row-level data -- just counts and a timestamp.
-- ============================================================
drop view if exists v_watchlist_meta;
create view v_watchlist_meta with (security_invoker = on) as
select
  (select count(*) from companies where active = true)                   as companies_tracked,
  (select count(distinct ats_type) from companies where active = true)   as ats_platforms_covered,
  (select max(scored_at) from matches)                                   as last_scored_at;

grant select on v_watchlist_meta to anon, authenticated;

-- ============================================================
-- Public view: open-role count per ATS platform, for the market-snapshot
-- section. Company names/notes are not exposed -- just a count per platform.
-- ============================================================
drop view if exists v_ats_coverage;
create view v_ats_coverage with (security_invoker = on) as
select
  c.ats_type                                as ats_type,
  count(*) filter (where c.active)          as companies_tracked,
  count(*) filter (
    where c.active and j.status = 'open'
  )                                          as open_roles
from companies c
left join jobs j on j.company_id = c.id
group by c.ats_type;

grant select on v_ats_coverage to anon, authenticated;

-- ============================================================
-- Revoke direct public read on the raw tables. RLS is already enabled on
-- all three (002_rls_policies.sql); dropping their only permissive policy
-- makes SELECT deny-by-default for anon/authenticated. v_watchlist,
-- v_watchlist_meta, and v_ats_coverage remain the entire public read
-- surface -- everything the frontend needs is served by those three views.
-- ============================================================
drop policy if exists "public read companies" on companies;
drop policy if exists "public read jobs" on jobs;
drop policy if exists "public read matches" on matches;
