#!/usr/bin/env python3
"""
run_eval.py
-----------
Scores every hand-labeled example in eval/golden_set.yaml through the REAL
production scoring functions (imported directly from fetch_and_score.py --
never reimplemented, so this can't silently drift from what the daily
pipeline actually does) and computes accuracy metrics against the expected
labels. See eval/README.md for the labeling rubric and how to add examples.

Escalation to stage 2 (Sonnet) follows the same STAGE1_PASS threshold
production uses -- but unlike score_job(), which only returns the final
(confirmed-or-screen) verdict, this keeps BOTH the stage1 and stage2 results
for every escalated example, so it can report a stage1-vs-stage2 disagreement
rate among exactly the examples that would have escalated in production.

Env vars:
  ANTHROPIC_API_KEY                     required, always
  SUPABASE_URL / SUPABASE_SERVICE_KEY   required unless --dry-run

Usage:
  python scripts/run_eval.py                    # real run, writes to Supabase
  python scripts/run_eval.py --dry-run           # scores for real, doesn't write
  python scripts/run_eval.py --golden-set path/to/other_set.yaml
"""

import argparse, logging, math, sys, uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

import fetch_and_score as fs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("watchlist.eval")

DEFAULT_GOLDEN_SET = Path(__file__).resolve().parent.parent / "eval" / "golden_set.yaml"
SUPPORTED_SCHEMA_VERSION = 1
REQUIRED_INPUT_FIELDS = ("title", "location", "raw_jd")
REQUIRED_EXPECTED_FIELDS = (
    "score_range", "role_fit", "level_fit", "location_fit",
    "meets_experience", "blocker",
)


# ── Golden set loading + validation ─────────────────────────────────────────

def load_golden_set(path: Path) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"Golden set not found: {path}")
    with open(path) as f:
        doc = yaml.safe_load(f) or {}

    version = doc.get("schema_version")
    if version != SUPPORTED_SCHEMA_VERSION:
        raise SystemExit(
            f"{path}: schema_version {version!r} is not supported "
            f"(expected {SUPPORTED_SCHEMA_VERSION})"
        )

    examples = doc.get("examples") or []
    if not examples:
        raise SystemExit(f"{path}: no examples found under 'examples:'")

    seen_ids: set[str] = set()
    for ex in examples:
        ex_id = ex.get("id")
        if not ex_id:
            raise SystemExit(f"{path}: an example is missing 'id'")
        if ex_id in seen_ids:
            raise SystemExit(f"{path}: duplicate example id {ex_id!r}")
        seen_ids.add(ex_id)

        inp = ex.get("input") or {}
        for field in REQUIRED_INPUT_FIELDS:
            if field not in inp:
                raise SystemExit(f"{ex_id}: missing input.{field}")

        exp = ex.get("expected") or {}
        for field in REQUIRED_EXPECTED_FIELDS:
            if field not in exp:
                raise SystemExit(f"{ex_id}: missing expected.{field}")

        score_range = exp["score_range"]
        if (
            not isinstance(score_range, list)
            or len(score_range) != 2
            or score_range[0] > score_range[1]
        ):
            raise SystemExit(f"{ex_id}: expected.score_range must be [min, max]")

    log.info(f"Loaded {len(examples)} examples from {path}")
    return examples


# ── Scoring ──────────────────────────────────────────────────────────────────

def score_example(ex: dict) -> dict:
    """Mirrors score_job()'s escalation logic (stage2 only runs if stage1
    clears STAGE1_PASS), but -- unlike score_job(), which discards the stage1
    verdict once stage2 confirms -- keeps both. Returns a flat dict of
    everything the metrics + eval_examples row need."""
    inp = ex["input"]
    title, location, raw_jd = inp["title"], inp["location"], inp["raw_jd"]
    role_family = inp.get("role_family_guess")

    stage1 = fs._call_model(fs.SCORE_MODEL_STAGE1, title, location, raw_jd, role_family)
    if stage1 is None:
        raise RuntimeError(f"{ex['id']}: stage1 (Haiku) call failed -- see warning above")

    stage2 = None
    if int(stage1.get("score", 0)) >= fs.STAGE1_PASS:
        stage2 = fs._call_model(fs.SCORE_MODEL_STAGE2, title, location, raw_jd, role_family)

    final = stage2 if stage2 is not None else stage1

    return {
        "golden_id": ex["id"],
        "company_name": (ex.get("source") or {}).get("company_name"),
        "job_title": title,
        "expected_score_min": ex["expected"]["score_range"][0],
        "expected_score_max": ex["expected"]["score_range"][1],
        "actual_score": final.get("score"),
        "expected_role_fit": ex["expected"]["role_fit"],
        "actual_role_fit": final.get("role_fit"),
        "expected_level_fit": ex["expected"]["level_fit"],
        "actual_level_fit": final.get("level_fit"),
        "expected_location_fit": ex["expected"]["location_fit"],
        "actual_location_fit": final.get("location_fit"),
        "guardrail_expected_fire": _guard_should_fire(ex["expected"]),
        "guardrail_actual_fire": _guard_should_fire(final),
        "stage1_score": stage1.get("score"),
        "stage2_score": stage2.get("score") if stage2 is not None else None,
        "reasoning": final.get("reasoning"),
    }


def _guard_should_fire(fields: dict) -> bool:
    """Mirrors _apply_score_guards()'s trigger condition: blocker, weak
    level_fit, or an explicit experience miss. Applied identically to a golden
    example's `expected` block and to a model verdict's own fields, so the two
    are directly comparable."""
    return bool(
        fields.get("blocker") is True
        or fields.get("level_fit") == "weak"
        or fields.get("meets_experience") is False
    )


# ── Metrics ──────────────────────────────────────────────────────────────────

def compute_metrics(rows: list[dict]) -> dict:
    n = len(rows)

    def midpoint(r):
        return (r["expected_score_min"] + r["expected_score_max"]) / 2

    mids = [midpoint(r) for r in rows]
    actuals = [r["actual_score"] for r in rows]

    mae = sum(abs(a - m) for a, m in zip(actuals, mids)) / n
    within_range = sum(
        1 for r in rows
        if r["expected_score_min"] <= r["actual_score"] <= r["expected_score_max"]
    )
    pct_within = 100.0 * within_range / n

    correlation = _pearson(actuals, mids)

    def agreement(dim_expected: str, dim_actual: str) -> float:
        matches = sum(1 for r in rows if r[dim_expected] == r[dim_actual])
        return 100.0 * matches / n

    role_agree = agreement("expected_role_fit", "actual_role_fit")
    level_agree = agreement("expected_level_fit", "actual_level_fit")
    location_agree = agreement("expected_location_fit", "actual_location_fit")

    guard_correct = sum(
        1 for r in rows if r["guardrail_expected_fire"] == r["guardrail_actual_fire"]
    )
    guard_correct_rate = 100.0 * guard_correct / n

    escalated = [r for r in rows if r["stage2_score"] is not None]
    if escalated:
        disagreements = sum(1 for r in escalated if _stages_disagree(r))
        stage_disagreement_rate = 100.0 * disagreements / len(escalated)
    else:
        stage_disagreement_rate = None

    return {
        "example_count": n,
        "score_mae": round(mae, 2),
        "score_correlation": round(correlation, 3) if correlation is not None else None,
        "pct_within_expected_range": round(pct_within, 1),
        "role_fit_agreement_rate": round(role_agree, 1),
        "level_fit_agreement_rate": round(level_agree, 1),
        "location_fit_agreement_rate": round(location_agree, 1),
        "guardrail_correct_rate": round(guard_correct_rate, 1),
        "stage_disagreement_rate": (
            round(stage_disagreement_rate, 1) if stage_disagreement_rate is not None else None
        ),
    }


def _stages_disagree(r: dict, score_gap_threshold: int = 15) -> bool:
    """A stage1/stage2 pair 'disagrees' if their raw scores are far enough
    apart to plausibly change the outcome. Only scores are compared per-stage
    today (fit labels aren't stored separately per stage -- see the
    eval_examples schema) -- adjust score_gap_threshold if it proves too
    loose/tight once real data exists."""
    return abs((r["stage1_score"] or 0) - (r["stage2_score"] or 0)) >= score_gap_threshold


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    varx = sum((x - mx) ** 2 for x in xs)
    vary = sum((y - my) ** 2 for y in ys)
    if varx == 0 or vary == 0:
        return None
    return cov / math.sqrt(varx * vary)


# ── Supabase write ───────────────────────────────────────────────────────────

def write_results(run_id: str, metrics: dict, example_rows: list[dict], golden_set_version: str):
    run_row = {
        "id": run_id,
        "run_at": datetime.now(timezone.utc).isoformat(),
        "golden_set_version": golden_set_version,
        "example_count": metrics["example_count"],
        "score_model_stage1": fs.SCORE_MODEL_STAGE1,
        "score_model_stage2": fs.SCORE_MODEL_STAGE2,
        "score_mae": metrics["score_mae"],
        "score_correlation": metrics["score_correlation"],
        "pct_within_expected_range": metrics["pct_within_expected_range"],
        "role_fit_agreement_rate": metrics["role_fit_agreement_rate"],
        "level_fit_agreement_rate": metrics["level_fit_agreement_rate"],
        "location_fit_agreement_rate": metrics["location_fit_agreement_rate"],
        "guardrail_correct_rate": metrics["guardrail_correct_rate"],
        "stage_disagreement_rate": metrics["stage_disagreement_rate"],
    }
    fs.sb_upsert("eval_runs", [run_row], "id")

    example_table_rows = [{**row, "id": str(uuid.uuid4()), "run_id": run_id} for row in example_rows]
    fs.sb_upsert("eval_examples", example_table_rows, "id")


# ── Reporting ────────────────────────────────────────────────────────────────

def print_summary(metrics: dict, example_rows: list[dict]):
    log.info("=== Eval results ===")
    log.info(f"  examples:                {metrics['example_count']}")
    log.info(f"  score MAE:               {metrics['score_mae']}")
    log.info(f"  score correlation:       {metrics['score_correlation']}")
    log.info(f"  % within expected range: {metrics['pct_within_expected_range']}%")
    log.info(f"  role_fit agreement:      {metrics['role_fit_agreement_rate']}%")
    log.info(f"  level_fit agreement:     {metrics['level_fit_agreement_rate']}%")
    log.info(f"  location_fit agreement:  {metrics['location_fit_agreement_rate']}%")
    log.info(f"  guardrail correct rate:  {metrics['guardrail_correct_rate']}%")
    log.info(f"  stage1/stage2 disagree:  {metrics['stage_disagreement_rate']}%")
    for r in example_rows:
        flag = "OK" if r["expected_score_min"] <= r["actual_score"] <= r["expected_score_max"] else "MISS"
        log.info(
            f"  [{flag:4}] {r['golden_id']}: expected "
            f"[{r['expected_score_min']}-{r['expected_score_max']}], got {r['actual_score']} "
            f"(role={r['actual_role_fit']} level={r['actual_level_fit']} loc={r['actual_location_fit']})"
        )


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden-set", type=Path, default=DEFAULT_GOLDEN_SET)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Score for real (still calls the model) but don't write results to Supabase.",
    )
    args = parser.parse_args()

    fs._load_anthropic_key()
    if not args.dry_run:
        fs._load_supabase_config()

    examples = load_golden_set(args.golden_set)

    example_rows = []
    for ex in examples:
        log.info(f"Scoring {ex['id']} ({ex['input']['title']})...")
        try:
            example_rows.append(score_example(ex))
        except RuntimeError as e:
            log.error(str(e))
            sys.exit(1)

    metrics = compute_metrics(example_rows)
    print_summary(metrics, example_rows)

    if args.dry_run:
        log.info("--dry-run: skipping Supabase write.")
        return

    run_id = str(uuid.uuid4())
    golden_set_version = f"1@{args.golden_set.stat().st_mtime_ns}"
    write_results(run_id, metrics, example_rows, golden_set_version)
    log.info(f"Wrote run {run_id} to Supabase (eval_runs, eval_examples).")


if __name__ == "__main__":
    main()
