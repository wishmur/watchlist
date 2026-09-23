-- 009_fix_public_view_access.sql
-- Fixes the public board returning zero rows to every visitor.
--
-- THE BUG
-- -------
-- Every public view was created `with (security_invoker = on)`, which makes it
-- execute using the *caller's* permissions. sql/005 then revoked anon's SELECT
-- policies on companies / jobs / matches. The combination is fatal and silent:
-- anon queries v_jobs_us, the view reads `jobs` as anon, RLS returns no rows,
-- and the view hands back an empty set with HTTP 200. No error anywhere.
--
-- Observed live: service key sees 225 open roles, anon sees 0. The legacy
-- v_watchlist has been equally broken since 005 was applied.
--
-- security_invoker = on was added deliberately in sql/001, with the reasoning
-- that it stops anon seeing rows RLS should hide. That reasoning applied when
-- the base tables carried permissive `using (true)` policies and the view was
-- an extra filter on top. It is exactly backwards now: the base tables are
-- deny-by-default and the view IS the security boundary.
--
-- THE FIX
-- -------
-- Recreate the public views WITHOUT security_invoker, so they run as the view
-- owner and can read the base tables, while anon still only ever sees the
-- columns and rows the view defines. Base-table grants stay revoked — nothing
-- from 005 is undone. This is the standard "curated view over restricted
-- tables" pattern, and it is safe here precisely because v_jobs_public already
-- excludes raw_jd and every candidate-specific field.
--
-- Idempotent: safe to rerun. Verify with scripts/check_schema.py, which now
-- asserts anon actually receives ROWS rather than just a 200.

-- ============================================================
-- The board. Same definition as 008 minus security_invoker.
-- ============================================================
-- Drop in dependency order: dependents first, base last. The graph is
--   v_board_meta -> v_jobs_us / v_jobs_intl -> v_jobs_public
-- so dropping v_jobs_us before v_board_meta fails with 2BP01. Explicit order
-- rather than CASCADE, so this can never silently take out a view that is not
-- listed here.
drop view if exists v_board_meta;
drop view if exists v_jobs_us;
drop view if exists v_jobs_intl;
drop view if exists v_jobs_public;

create view v_jobs_public as
select
  j.id                        as job_id,
  c.name                      as company_name,
  c.ats_type                  as source_ats,
  j.title                     as title,
  j.url                       as job_url,
  j.locations                 as locations,
  j.countries                 as countries,
  j.board_scope               as board_scope,
  j.remote_region             as remote_region,
  j.workplace_type            as workplace_type,
  j.is_remote                 as is_remote,
  j.department                as department,
  j.employment_type           as employment_type,
  j.posted_at                 as posted_at,
  j.first_seen_at             as first_seen_at,
  coalesce(f.seniority, j.seniority)         as seniority,
  f.years_required_min        as years_required_min,
  f.years_required_max        as years_required_max,
  f.experience_adjacency_allowed as experience_adjacency_allowed,
  f.technical_depth           as technical_depth,
  f.domain_tags               as domain_tags,
  f.company_industry          as company_industry,
  f.sponsorship_mentioned     as sponsorship_mentioned,
  coalesce(j.comp_min, f.comp_min)           as comp_min,
  coalesce(j.comp_max, f.comp_max)           as comp_max,
  coalesce(j.comp_currency, f.comp_currency) as comp_currency,
  (
    (case when coalesce(j.comp_min, f.comp_min) is not null then 2 else 0 end)
  + (case when coalesce(f.seniority, j.seniority) is not null then 1 else 0 end)
  + (case when f.years_required_min is not null then 1 else 0 end)
  + (case when f.sponsorship_mentioned = 'offered' then 1 else 0 end)
  + (case when array_length(f.domain_tags, 1) > 0 then 1 else 0 end)
  + (case when j.workplace_type is not null then 1 else 0 end)
  )                           as completeness
from jobs j
join companies c    on c.id = j.company_id
join job_facts f    on f.company_id = j.company_id and f.content_hash = j.content_hash
where j.status = 'open'
  and f.is_pm_role is true
  and j.board_scope is distinct from null;

grant select on v_jobs_public to anon, authenticated;

create view v_jobs_us as
  select * from v_jobs_public where board_scope in ('us', 'both', 'unknown');
grant select on v_jobs_us to anon, authenticated;

create view v_jobs_intl as
  select * from v_jobs_public where board_scope in ('intl', 'both', 'unknown');
grant select on v_jobs_intl to anon, authenticated;

create view v_board_meta as
select
  (select count(*) from v_jobs_public)                     as open_roles,
  (select count(*) from v_jobs_us)                         as us_roles,
  (select count(*) from v_jobs_intl)                       as intl_roles,
  (select count(distinct company_name) from v_jobs_public) as companies,
  (select count(*) from companies where active = true)     as companies_tracked,
  (select max(extracted_at) from job_facts)                as last_classified_at;

grant select on v_board_meta to anon, authenticated;

-- ============================================================
-- Eval summary. classification_eval_runs has RLS on with no policy, so an
-- invoker-rights view over it is permanently empty for anon too.
-- ============================================================
drop view if exists v_eval_latest;
create view v_eval_latest as
select
  run_at, golden_set_version, taxonomy_version, model, example_count,
  precision, recall, f1, accuracy, reason_accuracy
from classification_eval_runs
order by run_at desc
limit 1;

grant select on v_eval_latest to anon, authenticated;

-- ============================================================
-- Legacy board, same bug, same fix. Keeps the existing site working until the
-- cutover; 010 drops this along with `matches`.
--
-- Note this view exposes matches.reasoning, which is candidate-specific text.
-- That is acceptable only because it is about to be removed. Do not copy this
-- view's shape into anything new.
-- ============================================================
drop view if exists v_watchlist;
create view v_watchlist as
select
  j.id              as job_id,
  c.id              as company_id,
  c.name            as company_name,
  c.tier            as company_tier,
  c.ats_type        as ats_type,
  j.title           as job_title,
  j.location        as location,
  j.url             as job_url,
  j.posted_at       as posted_at,
  j.first_seen_at   as first_seen_at,
  j.status          as job_status,
  m.score           as score,
  m.role_fit        as role_fit,
  m.level_fit       as level_fit,
  m.location_fit    as location_fit,
  m.reasoning       as reasoning,
  m.scored_at       as scored_at
from jobs j
join companies c on c.id = j.company_id
join matches m on m.job_id = j.id
where j.status = 'open'
  and m.score >= 65;

grant select on v_watchlist to anon, authenticated;
