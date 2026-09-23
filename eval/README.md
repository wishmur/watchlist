# Evaluation

A small, hand-labeled golden set for measuring the scorer's accuracy against
real (or realistic) postings, run via `scripts/run_eval.py`.

## Why this exists

The daily pipeline (`scripts/fetch_and_score.py`) scores every posting with an
LLM, but until this existed there was no way to check whether those scores
were actually *right* -- no golden set, no precision/recall, just one informal
one-off spot-check. This is a first, deliberately small step toward a real
accuracy signal, not a finished evaluation suite. See `golden_set.yaml`'s
header for its current status (synthetic smoke-test examples vs. real
hand-labeled ones).

## Files

- `golden_set.yaml` -- the labeled examples themselves.
- `../scripts/run_eval.py` -- scores every example through the real production
  scoring functions (imported directly from `fetch_and_score.py`, never
  reimplemented) and computes accuracy metrics.

## The labeling rubric

An example's `expected` block must be labeled the same way
`fetch_and_score.py`'s `SCORE_SYSTEM` prompt and `profile/candidate_profile.md`
would judge it -- **not** your own independent opinion of the posting. If the
rubric or the candidate profile change, re-check existing labels against the
new rubric before trusting the eval's output; a stale label measures agreement
with a rubric that no longer exists.

- **`score_range`**: a `[min, max]` band, not a single number. The scoring
  formula is deterministic once you know the three fit labels (see
  `SCORE_SYSTEM`'s formula), so pick the band the correct fit labels would
  produce, plus a little slack for reasoning variance -- don't hand-pick an
  exact integer, that would fake more precision than the rubric has.
- **`role_fit` / `level_fit` / `location_fit`**: `strong` / `moderate` / `weak`,
  per the dimension rubrics in `SCORE_SYSTEM`.
- **`meets_experience`**: apply `candidate_profile.md`'s years-of-experience
  rule literally -- adjacency-clause language ("PM or equivalent/technical/
  adjacent experience") passes even at a 5-7 year floor; a rigid pure-PM bar
  with no adjacency clause, especially 7+ years, does not.
- **`blocker`**: `true` only for US citizenship, active security clearance,
  green card/permanent residency, or explicit "no sponsorship ever" language.
  A role that's simply non-US or requires relocation outside the US is a
  `location_fit: weak` case, **not** a blocker -- these are easy to conflate
  and worth double-checking (see `gs-0004` in `golden_set.yaml` for a worked
  example of the distinction).
- **`rationale`**: cite the specific JD language driving your labels. This is
  what makes a label auditable later, especially once the rubric or the
  candidate's situation changes.

## Adding a real example

1. Pick a posting from Supabase's `jobs`/`matches` tables (needs the
   service-role key -- these are not in the public `v_watchlist` view) or any
   real posting you've seen.
2. Copy the verbatim JD text into `input.raw_jd` (trimmed the same way the
   pipeline trims it, i.e. don't bother including more than
   `JD_MAX_CHARS` -- default 3000 -- characters, since that's all the real
   scorer ever sees).
3. Label `expected.*` yourself, using the rubric above, before looking at what
   the model actually scored it (if you already know the model's answer,
   labeling toward it defeats the point).
4. Set `synthetic: false` (or omit the field -- it defaults to not-synthetic)
   and give it the next `gs-NNNN` id.
5. Run `python3 scripts/run_eval.py --dry-run` first to sanity-check the file
   parses, then a real run to see how the model actually did against it.

## Running

```bash
# From the shay-watchlist repo root, with ANTHROPIC_API_KEY set (and
# SUPABASE_URL / SUPABASE_SERVICE_KEY set unless --dry-run):
python3 scripts/run_eval.py

# --dry-run skips writing results to Supabase (still calls the real model --
# this is not a mocked run, just a non-persisting one). Useful while iterating
# on golden_set.yaml or run_eval.py itself without touching production data.
python3 scripts/run_eval.py --dry-run
```

Each run calls the model the same way production's escalation logic would --
one Haiku call per example, plus one Sonnet call for each that clears
`STAGE1_PASS` (~most of a well-curated golden set, since these are meant to be
realistic postings, not obvious rejects). At a golden set of 15-30 examples
that's roughly 20-60 calls; re-run manually when the golden set changes or the
scoring prompt/thresholds change, not on a schedule.
