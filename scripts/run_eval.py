#!/usr/bin/env python3
"""
run_eval.py
-----------
Measures the classifier's PM / not-PM decision against eval/golden_set.yaml.

What changed and why
--------------------
This used to measure agreement with a personal fit rubric -- score MAE against
one candidate's profile -- which is not a question a public board has. The only
accuracy question now is whether a row shown as product management really is,
so this reports precision and recall on is_pm_role.

Precision is the headline. A board can afford to miss a role; it cannot afford
to tell someone a solutions architect job is product management. That asymmetry
is why the golden set deliberately oversamples FDE, solutions and customer
engineering titles -- the families that made up 45% of the previous board.

Calls the real production path (classify.call_model with the real tool schema
and system prompt), never a reimplementation, so a prompt change shows up here.

Usage:
    python scripts/run_eval.py --dry-run     # show the set, make no API calls
    python scripts/run_eval.py               # run and write results
    python scripts/run_eval.py --no-write    # run, print, write nothing
"""

from __future__ import annotations

import argparse
import collections
import logging
import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import local_env  # noqa: F401  -- loads .env for local runs

import classify
import taxonomy

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("eval")

GOLDEN_SET = Path(__file__).resolve().parent.parent / "eval" / "golden_set.yaml"


def load_golden_set(path: Path) -> tuple[list[dict], str]:
    with open(path) as f:
        doc = yaml.safe_load(f)
    examples = doc.get("examples") or []
    if not examples:
        raise SystemExit(f"{path} contains no examples")
    version = f"v{doc.get('schema_version', 1)}@{path.stat().st_mtime_ns}"
    return examples, version


def evaluate(examples: list[dict], spend: classify.Spend) -> list[dict]:
    results = []
    for n, ex in enumerate(examples, 1):
        inp = ex["input"]
        out = classify.call_model(
            classify.MODEL, inp["title"], [inp.get("location") or ""],
            ex.get("source", {}).get("department"), inp.get("raw_jd") or "", spend,
        )
        if out is None:
            log.warning(f"no output for {ex['id']} ({inp['title'][:50]!r}) -- skipped")
            continue
        expected = ex["expected"]
        actual_is_pm = bool(out.get("is_pm_role"))
        actual_reason = None if actual_is_pm else (out.get("exclusion_reason") or "other")
        results.append({
            "golden_id": ex["id"],
            "title": inp["title"],
            "company_name": ex.get("source", {}).get("company_name"),
            "expected_is_pm": bool(expected["is_pm_role"]),
            "actual_is_pm": actual_is_pm,
            "expected_reason": expected.get("exclusion_reason"),
            "actual_reason": actual_reason,
            "correct": actual_is_pm == bool(expected["is_pm_role"]),
            "confidence": out.get("confidence"),
            "note": (out.get("classification_note") or "")[:200] or None,
        })
        if n % 20 == 0:
            log.info(f"  {n}/{len(examples)}  ${spend.usd:.3f}")
    return results


def compute_metrics(rows: list[dict]) -> dict:
    tp = sum(1 for r in rows if r["expected_is_pm"] and r["actual_is_pm"])
    fp = sum(1 for r in rows if not r["expected_is_pm"] and r["actual_is_pm"])
    tn = sum(1 for r in rows if not r["expected_is_pm"] and not r["actual_is_pm"])
    fn = sum(1 for r in rows if r["expected_is_pm"] and not r["actual_is_pm"])
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    f1 = (2 * precision * recall / (precision + recall)) if (precision and recall) else None
    accuracy = (tp + tn) / len(rows) if rows else None

    # Of the rejections we got right, how often did we reject for the right reason?
    # A role dropped as "design" when it is really presales is still the right
    # call for the board, but it means the taxonomy is not being applied cleanly.
    correct_rejections = [r for r in rows
                          if not r["expected_is_pm"] and not r["actual_is_pm"]
                          and r["expected_reason"]]
    reason_hits = sum(1 for r in correct_rejections if r["actual_reason"] == r["expected_reason"])
    reason_accuracy = reason_hits / len(correct_rejections) if correct_rejections else None

    return {
        "true_positives": tp, "false_positives": fp,
        "true_negatives": tn, "false_negatives": fn,
        "precision": precision, "recall": recall, "f1": f1,
        "accuracy": accuracy, "reason_accuracy": reason_accuracy,
    }


def print_summary(m: dict, rows: list[dict]) -> None:
    pct = lambda v: "n/a" if v is None else f"{v*100:.1f}%"  # noqa: E731
    print("\n" + "=" * 62)
    print(f"  examples evaluated   {len(rows)}")
    print(f"  PRECISION            {pct(m['precision'])}   <- the one that matters")
    print(f"  recall               {pct(m['recall'])}")
    print(f"  f1                   {pct(m['f1'])}")
    print(f"  accuracy             {pct(m['accuracy'])}")
    print(f"  reason accuracy      {pct(m['reason_accuracy'])}")
    print(f"  tp/fp/tn/fn          {m['true_positives']}/{m['false_positives']}"
          f"/{m['true_negatives']}/{m['false_negatives']}")
    print("=" * 62)

    fps = [r for r in rows if not r["expected_is_pm"] and r["actual_is_pm"]]
    fns = [r for r in rows if r["expected_is_pm"] and not r["actual_is_pm"]]
    if fps:
        print("\nFALSE POSITIVES (shown as PM, should not be) -- these are the costly ones:")
        for r in fps:
            print(f"  {r['title'][:60]:<62} expected {r['expected_reason']}")
            if r["note"]:
                print(f"     model said: {r['note'][:90]}")
    if fns:
        print("\nFALSE NEGATIVES (real PM roles dropped):")
        for r in fns:
            print(f"  {r['title'][:60]:<62} model said {r['actual_reason']}")
            if r["note"]:
                print(f"     model said: {r['note'][:90]}")
    if not fps and not fns:
        print("\nno misclassifications on this set.")

    by_reason = collections.Counter(
        r["actual_reason"] for r in rows if not r["actual_is_pm"] and r["actual_reason"])
    if by_reason:
        print(f"\nrejection reasons: {dict(by_reason)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden-set", type=Path, default=GOLDEN_SET)
    ap.add_argument("--dry-run", action="store_true", help="list the set, make no API calls")
    ap.add_argument("--no-write", action="store_true", help="run but do not persist results")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    examples, version = load_golden_set(args.golden_set)
    if args.limit:
        examples = examples[:args.limit]

    pos = sum(1 for e in examples if e["expected"]["is_pm_role"])
    review = sum(1 for e in examples if e.get("needs_review"))
    log.info(f"{len(examples)} examples ({pos} PM, {len(examples)-pos} not PM, "
             f"{review} flagged for human review) from {args.golden_set.name}")

    if args.dry_run:
        est = classify.estimate_usd(len(examples))
        log.info(f"DRY RUN: would cost about ${est:.2f} with {classify.MODEL}")
        by = collections.Counter(
            e["expected"].get("exclusion_reason") or "pm" for e in examples)
        log.info(f"label distribution: {dict(by)}")
        return 0

    if not classify.ANTHROPIC_KEY:
        log.error("ANTHROPIC_API_KEY is required")
        return 2

    spend = classify.Spend()
    rows = evaluate(examples, spend)
    metrics = compute_metrics(rows)
    print_summary(metrics, rows)
    log.info(f"{spend.calls} calls, ${spend.usd:.3f}")

    if args.no_write:
        return 0
    if not classify.SUPABASE_URL or not classify.SUPABASE_KEY:
        log.warning("no Supabase credentials; results not persisted")
        return 0

    try:
        classify.sb_upsert("classification_eval_runs", [{
            "golden_set_version": version,
            "taxonomy_version": taxonomy.TAXONOMY_VERSION,
            "model": classify.MODEL,
            "example_count": len(rows),
            **{k: v for k, v in metrics.items()},
        }], "id")
        log.info("results written to classification_eval_runs")
    except Exception as e:
        log.warning(f"could not persist results: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
