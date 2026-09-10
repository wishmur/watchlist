-- 007_eval_results.sql
-- Storage for scripts/run_eval.py's golden-set eval runs.
--
-- Mirrors 005_public_read_hardening.sql's exact pattern: raw tables are
-- RLS-locked with no public grant, and one curated aggregate view is exposed
-- to anon/authenticated. Per-example drill-down (eval_examples -- which
-- includes model `reasoning` text, the same kind of text 005 already
-- restricted on `matches` for revealing candidate-specific screening
-- criteria) is deliberately NOT exposed publicly here, even in aggregate --
-- only eval_runs' summary metrics are.
--
-- run_eval.py writes both tables with the service_role key via the existing
-- sb_upsert() helper in fetch_and_score.py, the same way the daily pipeline
-- writes jobs/matches. It is a manual/on-demand script, not part of the
-- scheduled GitHub Actions workflows.
--
-- Idempotent: safe to rerun. Run once in the Supabase SQL editor.

create table if not exists eval_runs (
  id                           uuid primary key default gen_random_uuid(),
  run_at                       timestamptz not null default now(),
  golden_set_version           text not null,
  example_count                integer not null,
  score_model_stage1           text not null,
  score_model_stage2           text not null,
  score_mae                    numeric,
  score_correlation            numeric,
  pct_within_expected_range    numeric,
  role_fit_agreement_rate      numeric,
  level_fit_agreement_rate     numeric,
  location_fit_agreement_rate  numeric,
  guardrail_correct_rate       numeric,
  stage_disagreement_rate      numeric
);

create table if not exists eval_examples (
  id                      uuid primary key default gen_random_uuid(),
  run_id                  uuid not null references eval_runs(id) on delete cascade,
  golden_id               text not null,
  company_name            text,
  job_title               text,
  expected_score_min      integer,
  expected_score_max      integer,
  actual_score            integer,
  expected_role_fit       text,
  actual_role_fit         text,
  expected_level_fit      text,
  actual_level_fit        text,
  expected_location_fit   text,
  actual_location_fit     text,
  guardrail_expected_fire boolean,
  guardrail_actual_fire   boolean,
  stage1_score            integer,
  stage2_score            integer,
  reasoning               text
);

alter table eval_runs enable row level security;
alter table eval_examples enable row level security;
-- No permissive policy on either table -- SELECT is deny-by-default for
-- anon/authenticated, matching companies/jobs/matches post-005. Only the
-- service_role key (used by run_eval.py) can read/write these directly.

create index if not exists idx_eval_examples_run on eval_examples (run_id);

-- ============================================================
-- v_eval_latest: curated public view, latest run's aggregate metrics only.
-- No row-level example data -- just the computed numbers, same discipline as
-- v_watchlist_meta/v_ats_coverage in 005.
--
-- Deliberately WITHOUT security_invoker: this view exists specifically to let
-- a less-privileged role (anon) read a curated slice of a table it otherwise
-- has zero RLS visibility into. security_invoker=on would run the view's own
-- query AS the calling role, which -- combined with eval_runs having no
-- permissive policy for anon -- means the view would see zero rows and
-- always return empty, no matter what's granted on the view itself. Running
-- as the view's owner (the default) is what lets it actually read the table;
-- the boundary anon can't cross is still enforced, because only this view
-- (not eval_runs/eval_examples) is ever granted to anon/authenticated.
-- (v_watchlist_meta/v_ats_coverage in 005 use security_invoker=on and, per
-- the same reasoning, likely have this same bug -- worth checking if you
-- ever wire those views up to the frontend; out of scope to fix here.)
-- ============================================================
drop view if exists v_eval_latest;
create view v_eval_latest as
select
  id, run_at, golden_set_version, example_count,
  score_model_stage1, score_model_stage2,
  score_mae, score_correlation, pct_within_expected_range,
  role_fit_agreement_rate, level_fit_agreement_rate, location_fit_agreement_rate,
  guardrail_correct_rate, stage_disagreement_rate
from eval_runs
order by run_at desc
limit 1;

grant select on v_eval_latest to anon, authenticated;
