-- 006_job_facts_and_profiles.sql
-- V1 of the general-purpose matching layer.
--
-- Architecture: the LLM understands each job ONCE, at ingestion, and stores
-- structured facts (job_facts). Matching a visitor's profile against those
-- facts is pure SQL -- no LLM call per user x job, so cost does not scale
-- with visitor count. See scripts/taxonomy.py for the canonical, versioned
-- vocabulary these fields draw from, and scripts/job_facts_extraction.py for
-- the extraction that populates job_facts.
--
-- Additive only. Does not touch or replace matches / v_watchlist, which keep
-- powering the existing personal board until this is validated end-to-end
-- and the pipeline is switched over.
--
-- Idempotent: safe to rerun. Run once in the Supabase SQL editor.
-- NOTE: written and reasoned through carefully but not executed against a
-- live Postgres/Supabase instance (no service-role access in this session) --
-- sanity-check in the SQL editor's dry-run / a staging project before relying
-- on it in production.

create extension if not exists pgcrypto;

-- ============================================================
-- content_hash on jobs: identifies "the same real posting" independent of
-- which ATS row it arrived as. Fixes a concrete Phase 0 finding -- companies
-- (Databricks in particular) cross-post the identical requisition once per
-- city, and today each becomes an independently-scored row. Keying job_facts
-- on (company_id, content_hash) means all such duplicates share ONE
-- extraction: guaranteed-identical output, zero marginal cost for clones.
-- ============================================================
alter table jobs add column if not exists content_hash text;

update jobs
set content_hash = encode(digest(lower(trim(title)) || '|' || coalesce(raw_jd, ''), 'sha256'), 'hex')
where content_hash is null;

alter table jobs alter column content_hash set not null;
create index if not exists idx_jobs_content_hash on jobs (company_id, content_hash);

-- ============================================================
-- job_facts: user-agnostic structured extraction, one row per distinct
-- (company, content) pair. Never references any specific candidate --
-- there isn't one at ingestion time, which is the whole point.
--
-- Dimensions are kept independent on purpose (role function, seniority/
-- management scope, domain, industry, location, work-auth are each their
-- own field) so matching logic can filter/rank on each separately instead
-- of a single fused judgment call.
-- ============================================================
create table if not exists job_facts (
  company_id                   uuid not null references companies(id) on delete cascade,
  content_hash                 text not null,

  -- FUNCTION -- independent of seniority, domain, industry.
  -- One of: core_pm | forward_deployed_builder | technical_delivery_or_presales
  --         | partner_channel | research_or_internal_engineering | null
  role_archetype               text,
  role_archetype_confidence    text not null default 'high'
                                  check (role_archetype_confidence in ('high','moderate','low')),
  classification_note          text,  -- e.g. "title hit FDE keywords but JD describes a design role"

  -- SENIORITY / MANAGEMENT SCOPE -- independent of function.
  -- One of: ic | ic_senior_staff_principal | people_manager | director_plus | null
  seniority_scope              text,
  years_required_min           integer,   -- raw, as literally stated; null if unstated -- never inferred from title
  experience_adjacency_allowed boolean,   -- JD explicitly accepts adjacent/equivalent experience
  seniority_evidence           text,      -- quote/paraphrase grounding the call, for auditability

  -- DOMAIN -- what this specific role's day-to-day work touches (role-level).
  role_domain_tags             text[] not null default '{}',

  -- INDUSTRY -- the company's business, independent of what this role does.
  company_industry             text,

  -- LOCATION / WORK AUTH -- independent of everything above.
  location_countries           text[] not null default '{}',
  remote_eligible               boolean,
  work_auth_constraints         text[] not null default '{}',
  work_auth_evidence             text,

  -- Versioning: text, not native enum/check-constrained types, so the
  -- taxonomy can evolve (new archetype, split domain tag, etc.) via an
  -- UPDATE + re-extraction backfill, not a schema migration.
  taxonomy_version                text not null,
  extraction_model_version        text not null,
  extracted_at                     timestamptz not null default now(),

  primary key (company_id, content_hash)
);

create index if not exists idx_job_facts_archetype on job_facts (role_archetype);
create index if not exists idx_job_facts_taxonomy_version on job_facts (taxonomy_version);

-- job_facts writes happen via the service_role key (same as jobs/matches
-- today) -- no RLS policy needed beyond enabling it deny-by-default, since
-- extracted facts are not personal data and can be read publicly like the
-- rest of the board.
alter table job_facts enable row level security;
drop policy if exists "public read job_facts" on job_facts;
create policy "public read job_facts" on job_facts for select to anon, authenticated using (true);

-- ============================================================
-- user_profiles: NO AUTH REQUIRED. A visitor submits a profile via
-- create_profile() below and gets back an unguessable UUIDv4 -- knowledge of
-- that id is the only access control (the same model as a share link).
-- user_id is nullable and unused today; it exists purely so a later auth
-- system can let someone claim an existing anonymous profile by linking it
-- to a real account, without a breaking schema change.
-- ============================================================
create table if not exists user_profiles (
  id                            uuid primary key default gen_random_uuid(),
  user_id                       uuid,  -- nullable; populated only if/when auth is added later

  -- BACKGROUND FACTS about the person, named for what they literally are.
  years_experience_total        integer,
  years_pm_experience           integer,
  years_technical_experience    integer,
  years_ai_ml_experience        integer,
  work_auth_status              text not null,
    -- us_citizen | green_card | h1b | opt_stem | other_visa | no_restriction

  -- HARD CONSTRAINTS -- preferences that filter, not facts about the person.
  work_auth_excludes            text[] not null default '{}',
  accepted_locations            text[] not null default '{}',
  seniority_target              text[] not null default '{ic,ic_senior_staff_principal}',
  excluded_role_archetypes      text[] not null default '{}',
  excluded_industries            text[] not null default '{}',

  -- SOFT PREFERENCES -- ranking weights, not filters.
  role_archetype_priority        jsonb not null default '{}',
  domain_tag_weights              jsonb not null default '{}',

  narrative_context               text,  -- free text; reserved for a future on-demand LLM "explain this match"
                                          -- call, NOT used in the deterministic ranking math below.
  profile_version                  integer not null default 1,
  created_at                       timestamptz not null default now(),
  updated_at                       timestamptz not null default now()
);

alter table user_profiles enable row level security;
-- Deliberately NO select/update/delete policy for anon/authenticated on the
-- raw table -- all access goes through the SECURITY DEFINER functions below.
-- This is the same "aggregate views, not raw tables" discipline as
-- 005_public_read_hardening.sql, applied to a table that now holds personal
-- background/visa information instead of curated scores.

-- ============================================================
-- create_profile / update_profile / get_profile: the only way to write or
-- read a user_profiles row. Each requires the caller to already hold the
-- profile's id (for update/get) -- there is no listing endpoint, so a
-- correctly-random UUID cannot be enumerated, only guessed one-at-a-time,
-- which is an acceptable v1 tradeoff for a no-login product (identical to
-- how a Google Doc or Stripe payment link works).
-- ============================================================
create or replace function create_profile(profile jsonb)
returns uuid
language plpgsql
security definer
set search_path = public
as $$
declare new_id uuid;
begin
  insert into user_profiles (
    years_experience_total, years_pm_experience, years_technical_experience,
    years_ai_ml_experience, work_auth_status, work_auth_excludes,
    accepted_locations, seniority_target, excluded_role_archetypes,
    excluded_industries, role_archetype_priority, domain_tag_weights, narrative_context
  ) values (
    (profile->>'years_experience_total')::int,
    (profile->>'years_pm_experience')::int,
    (profile->>'years_technical_experience')::int,
    (profile->>'years_ai_ml_experience')::int,
    profile->>'work_auth_status',
    coalesce((select array_agg(x) from jsonb_array_elements_text(coalesce(profile->'work_auth_excludes', '[]'::jsonb)) x), '{}'),
    coalesce((select array_agg(x) from jsonb_array_elements_text(coalesce(profile->'accepted_locations', '[]'::jsonb)) x), '{}'),
    coalesce((select array_agg(x) from jsonb_array_elements_text(coalesce(profile->'seniority_target', '[]'::jsonb)) x), '{ic,ic_senior_staff_principal}'),
    coalesce((select array_agg(x) from jsonb_array_elements_text(coalesce(profile->'excluded_role_archetypes', '[]'::jsonb)) x), '{}'),
    coalesce((select array_agg(x) from jsonb_array_elements_text(coalesce(profile->'excluded_industries', '[]'::jsonb)) x), '{}'),
    coalesce(profile->'role_archetype_priority', '{}'::jsonb),
    coalesce(profile->'domain_tag_weights', '{}'::jsonb),
    profile->>'narrative_context'
  )
  returning id into new_id;
  return new_id;
end;
$$;

grant execute on function create_profile(jsonb) to anon, authenticated;

create or replace function update_profile(p_id uuid, profile jsonb)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  update user_profiles set
    years_experience_total = coalesce((profile->>'years_experience_total')::int, years_experience_total),
    years_pm_experience = coalesce((profile->>'years_pm_experience')::int, years_pm_experience),
    years_technical_experience = coalesce((profile->>'years_technical_experience')::int, years_technical_experience),
    years_ai_ml_experience = coalesce((profile->>'years_ai_ml_experience')::int, years_ai_ml_experience),
    work_auth_status = coalesce(profile->>'work_auth_status', work_auth_status),
    work_auth_excludes = coalesce((select array_agg(x) from jsonb_array_elements_text(profile->'work_auth_excludes') x), work_auth_excludes),
    accepted_locations = coalesce((select array_agg(x) from jsonb_array_elements_text(profile->'accepted_locations') x), accepted_locations),
    seniority_target = coalesce((select array_agg(x) from jsonb_array_elements_text(profile->'seniority_target') x), seniority_target),
    excluded_role_archetypes = coalesce((select array_agg(x) from jsonb_array_elements_text(profile->'excluded_role_archetypes') x), excluded_role_archetypes),
    excluded_industries = coalesce((select array_agg(x) from jsonb_array_elements_text(profile->'excluded_industries') x), excluded_industries),
    role_archetype_priority = coalesce(profile->'role_archetype_priority', role_archetype_priority),
    domain_tag_weights = coalesce(profile->'domain_tag_weights', domain_tag_weights),
    narrative_context = coalesce(profile->>'narrative_context', narrative_context),
    profile_version = profile_version + 1,
    updated_at = now()
  where id = p_id;
end;
$$;

grant execute on function update_profile(uuid, jsonb) to anon, authenticated;

create or replace function get_profile(p_id uuid)
returns setof user_profiles
language sql
security definer
set search_path = public
as $$
  select * from user_profiles where id = p_id;
$$;

grant execute on function get_profile(uuid) to anon, authenticated;

-- ============================================================
-- get_matches: the actual matching/ranking. Pure SQL, computed live on
-- every call -- no precomputed per-profile match table, so there is nothing
-- to go stale as job_facts grows and no sync job to maintain. job_facts is
-- small enough (one row per distinct job) that the join is effectively
-- instant even at a few thousand rows.
-- ============================================================
create or replace function get_matches(p_profile_id uuid)
returns table (
  job_id uuid, company_name text, company_tier text, job_title text,
  location text, job_url text, posted_at timestamptz, first_seen_at timestamptz,
  role_archetype text, seniority_scope text, role_domain_tags text[],
  passes_hard_filters boolean, rank_score numeric
)
language sql
security definer
set search_path = public
as $$
  with up as (select * from user_profiles where id = p_profile_id)
  select
    j.id, c.name, c.tier, j.title, j.location, j.url, j.posted_at, j.first_seen_at,
    jf.role_archetype, jf.seniority_scope, jf.role_domain_tags,
    ( not (jf.work_auth_constraints && up.work_auth_excludes)
      and (jf.remote_eligible is true
           or jf.location_countries && up.accepted_locations
           or cardinality(jf.location_countries) = 0)
      and (jf.seniority_scope is null or jf.seniority_scope = any(up.seniority_target))
      and (jf.role_archetype is null or not (jf.role_archetype = any(up.excluded_role_archetypes)))
      and (jf.years_required_min is null
           or up.years_pm_experience >= jf.years_required_min
           or (jf.experience_adjacency_allowed
               and greatest(coalesce(up.years_technical_experience, 0),
                            coalesce(up.years_ai_ml_experience, 0),
                            coalesce(up.years_experience_total, 0)) >= jf.years_required_min)
      )
    ) as passes_hard_filters,
    ( coalesce((up.role_archetype_priority ->> jf.role_archetype)::numeric, 0.3)
      + coalesce((select sum((up.domain_tag_weights ->> tag)::numeric)
                  from unnest(jf.role_domain_tags) tag
                  where up.domain_tag_weights ? tag), 0)
    ) as rank_score
  from jobs j
  join companies c on c.id = j.company_id
  join job_facts jf on (jf.company_id, jf.content_hash) = (j.company_id, j.content_hash)
  cross join up
  where j.status = 'open'
  order by passes_hard_filters desc, rank_score desc;
$$;

grant execute on function get_matches(uuid) to anon, authenticated;
