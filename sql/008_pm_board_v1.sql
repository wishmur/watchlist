-- 008_pm_board_v1.sql
-- The public PM job board: classify once at ingestion, filter in SQL.
--
-- This supersedes the personal-fit model. `matches` held a score of how well a
-- posting fitted one specific candidate, which is meaningless to a visitor.
-- `job_facts` instead holds user-agnostic structured facts about the posting,
-- extracted once and cached by (company_id, content_hash), so matching and
-- filtering are pure SQL and cost nothing per visitor. That is what makes the
-- "no visitor action may trigger an LLM call" constraint structural rather
-- than a policy someone has to remember.
--
-- Supersedes sql/006, which designed this layer but was never applied -- its
-- job_facts and user_profiles tables return 404 on the live project. This is
-- that design, narrowed to the board's actual scope (core PM, seven countries,
-- no personal scoring) and extended with the filter dimensions the board needs.
--
-- Additive. `matches` and `v_watchlist` are left intact so the existing board
-- keeps serving until the cutover; 009 drops them.
--
-- NOT YET APPLIED. Dry-run on a Supabase branch before production.

create extension if not exists pgcrypto;

-- ============================================================
-- jobs: content hashing, richer location, structured ATS fields
--
-- content_hash identifies "the same real posting" independent of which ATS row
-- it arrived as. One company posts the identical requisition once per city --
-- 375 such rows exist today, one of them cross-posted 14 times -- and each
-- currently becomes an independently classified job. Keying job_facts on
-- (company_id, content_hash) means every clone shares ONE extraction:
-- identical output, zero marginal cost.
-- ============================================================
alter table jobs add column if not exists content_hash text;

update jobs
set content_hash = encode(
      digest(lower(trim(title)) || '|' || coalesce(raw_jd, ''), 'sha256'), 'hex')
where content_hash is null;

alter table jobs alter column content_hash set not null;
create index if not exists idx_jobs_content_hash on jobs (company_id, content_hash);

-- The full location list, not just whichever one the ATS printed first.
-- Workday collapses cross-posted reqs to "3 Locations"; ingest resolves that
-- via the detail endpoint and stores the real cities here.
alter table jobs add column if not exists locations        text[] not null default '{}';
alter table jobs add column if not exists countries        text[] not null default '{}';
alter table jobs add column if not exists board_scope      text
  check (board_scope in ('us', 'intl', 'both', 'unknown'));
alter table jobs add column if not exists remote_region    text
  check (remote_region in ('us', 'emea', 'unspecified'));

-- Deterministic, straight from the ATS -- never inferred by a model.
alter table jobs add column if not exists department       text;
alter table jobs add column if not exists team             text;
alter table jobs add column if not exists employment_type  text;
alter table jobs add column if not exists workplace_type   text;
alter table jobs add column if not exists is_remote        boolean;
alter table jobs add column if not exists comp_min         numeric;
alter table jobs add column if not exists comp_max         numeric;
alter table jobs add column if not exists comp_currency    text;

-- Gate 1 output, stored so the board can explain itself and so a taxonomy
-- change can be rerun over stored rows instead of re-fetching every ATS.
alter table jobs add column if not exists title_decision   text
  check (title_decision in ('include', 'exclude', 'uncertain'));
alter table jobs add column if not exists title_reason     text;
alter table jobs add column if not exists seniority        text
  check (seniority in ('associate', 'mid', 'senior', 'staff_principal', 'director_plus'));

-- Closed-detection support. A posting is only closed after it has been missing
-- from N consecutive SUCCESSFUL fetches. The old logic closed a company's whole
-- board on a single transient 500, because a failed fetch and an empty board
-- were indistinguishable.
alter table jobs add column if not exists missed_runs      integer not null default 0;
alter table jobs add column if not exists closed_at        timestamptz;

create index if not exists idx_jobs_board_scope on jobs (board_scope) where status = 'open';
create index if not exists idx_jobs_posted_at   on jobs (posted_at desc nulls last);
create index if not exists idx_jobs_countries   on jobs using gin (countries);

-- ============================================================
-- job_facts: user-agnostic extraction, one row per distinct posting content.
-- Never references a candidate -- there isn't one at ingestion time, which is
-- the entire point. Dimensions stay independent so the board can filter on
-- each separately rather than on one fused judgement.
-- ============================================================
create table if not exists job_facts (
  company_id                uuid not null references companies(id) on delete cascade,
  content_hash              text not null,

  -- Gate 2's verdict. is_pm_role is the board's single gate; exclusion_reason
  -- is what the eval measures precision against.
  is_pm_role                boolean not null,
  exclusion_reason          text
    check (exclusion_reason is null or exclusion_reason in (
      'program_management', 'product_marketing', 'solutions_or_fde', 'design',
      'engineering', 'analytics', 'operations', 'leadership', 'other')),
  confidence                text not null default 'high'
    check (confidence in ('high', 'moderate', 'low')),
  classification_note       text,   -- e.g. "title reads PM but JD describes presales"

  -- Filter dimensions.
  seniority                 text
    check (seniority is null or seniority in (
      'associate', 'mid', 'senior', 'staff_principal', 'director_plus')),
  years_required_min        integer,  -- as literally stated; never inferred from title
  years_required_max        integer,
  experience_adjacency_allowed boolean,
  technical_depth           text check (technical_depth is null or technical_depth in ('low', 'medium', 'high')),
  domain_tags               text[] not null default '{}',
  company_industry          text,
  sponsorship_mentioned     text
    check (sponsorship_mentioned is null or sponsorship_mentioned in ('offered', 'not_offered', 'unstated')),

  -- Compensation as stated in the JD, when the ATS didn't give it structurally.
  comp_min                  numeric,
  comp_max                  numeric,
  comp_currency             text,
  comp_source               text check (comp_source is null or comp_source in ('ats', 'jd_text', 'none')),

  -- Versioning as text, not enums, so the taxonomy can evolve via an UPDATE +
  -- targeted re-extraction rather than a schema migration. classify.py reruns
  -- any row whose version is stale -- the re-scoring path the old design lacked.
  taxonomy_version          text not null,
  extraction_model          text not null,
  extracted_at              timestamptz not null default now(),

  primary key (company_id, content_hash)
);

create index if not exists idx_job_facts_is_pm      on job_facts (is_pm_role);
create index if not exists idx_job_facts_seniority  on job_facts (seniority);
create index if not exists idx_job_facts_domains    on job_facts using gin (domain_tags);
create index if not exists idx_job_facts_version    on job_facts (taxonomy_version, extraction_model);

alter table job_facts enable row level security;
-- Extracted facts are not personal data; they are the board. Public read is
-- the intent here, unlike matches.reasoning which never should have been.
drop policy if exists "public read job_facts" on job_facts;
create policy "public read job_facts" on job_facts for select to anon, authenticated using (true);

-- ============================================================
-- pipeline_runs: spend and volume, queryable rather than inferred from Actions
-- logs. The per-run cap writes its accounting here.
-- ============================================================
create table if not exists pipeline_runs (
  id                  uuid primary key default gen_random_uuid(),
  stage               text not null check (stage in ('ingest', 'classify')),
  started_at          timestamptz not null default now(),
  finished_at         timestamptz,
  companies_seen      integer not null default 0,
  companies_failed    integer not null default 0,
  postings_listed     integer not null default 0,
  jobs_upserted       integer not null default 0,
  jobs_closed         integer not null default 0,
  classify_calls      integer not null default 0,
  input_tokens        bigint  not null default 0,
  output_tokens       bigint  not null default 0,
  estimated_usd       numeric not null default 0,
  hit_call_cap        boolean not null default false,
  hit_budget_cap      boolean not null default false,
  notes               text
);

create index if not exists idx_pipeline_runs_started on pipeline_runs (started_at desc);

alter table pipeline_runs enable row level security;
-- No public policy: spend data is not visitor-facing. service_role bypasses RLS.

-- ============================================================
-- v_jobs_public: the entire public read surface for the board.
--
-- Deliberately excludes raw_jd (full JD bodies) and anything candidate-specific.
-- security_invoker = on so RLS is evaluated as the caller, not the view owner.
-- ============================================================
drop view if exists v_jobs_public;
create view v_jobs_public with (security_invoker = on) as
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
  coalesce(f.seniority, j.seniority)      as seniority,
  f.years_required_min        as years_required_min,
  f.years_required_max        as years_required_max,
  f.experience_adjacency_allowed as experience_adjacency_allowed,
  f.technical_depth           as technical_depth,
  f.domain_tags               as domain_tags,
  f.company_industry          as company_industry,
  f.sponsorship_mentioned     as sponsorship_mentioned,
  coalesce(j.comp_min, f.comp_min)        as comp_min,
  coalesce(j.comp_max, f.comp_max)        as comp_max,
  coalesce(j.comp_currency, f.comp_currency) as comp_currency,
  -- Ranking input: freshness plus how completely the posting describes itself.
  -- Replaces the fit score. A req that states comp, seniority and sponsorship
  -- is more useful to a job seeker than one that states none of them.
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

-- ============================================================
-- Tab views. A requisition spanning both continents appears in BOTH, because
-- forcing it into one hides it from half the audience it was posted for.
-- 'unknown' (remote, no stated region) also appears in both rather than being
-- guessed into one.
-- ============================================================
drop view if exists v_jobs_us;
create view v_jobs_us with (security_invoker = on) as
  select * from v_jobs_public where board_scope in ('us', 'both', 'unknown');
grant select on v_jobs_us to anon, authenticated;

drop view if exists v_jobs_intl;
create view v_jobs_intl with (security_invoker = on) as
  select * from v_jobs_public where board_scope in ('intl', 'both', 'unknown');
grant select on v_jobs_intl to anon, authenticated;

-- ============================================================
-- v_board_meta: header counts, aggregate only, no row-level data.
-- ============================================================
drop view if exists v_board_meta;
create view v_board_meta with (security_invoker = on) as
select
  (select count(*) from v_jobs_public)                                as open_roles,
  (select count(*) from v_jobs_us)                                    as us_roles,
  (select count(*) from v_jobs_intl)                                  as intl_roles,
  (select count(distinct company_name) from v_jobs_public)            as companies,
  (select count(*) from companies where active = true)                as companies_tracked,
  (select max(extracted_at) from job_facts)                           as last_classified_at;

grant select on v_board_meta to anon, authenticated;
