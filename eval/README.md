# Evaluation

## Profile extraction

The shared dataset contains 50 labeled cases spanning concern groups, constraints,
negative examples, mixed concerns, and colloquial inputs.

```bash
python eval/run_eval.py --no-mlflow
```

Dataset labels are validated before any model request. See `LABELING.md` for the
labeling policy.

## Recommendation response quality

Response evaluation requires an OpenAI-compatible judge that is different from the
generation model and endpoint.

```bash
export JUDGE_MODEL="<external-model>"
export JUDGE_API_KEY="<api-key>"
# Optional for non-OpenAI compatible providers:
export JUDGE_BASE_URL="https://provider.example/v1"

python eval/run_response_eval.py \
  --gen-temperature 0 \
  --judge-repeats 3 \
  --out eval/results/external-judge.json
```

The runner refuses self-judging by default. `--allow-self-judge` exists only for
diagnostics and its scores must not be treated as unbiased quality measurements.

Each report records:

- generator/judge model and endpoint identities;
- generator/judge temperatures and prompt hashes;
- dataset SHA-256, sample count, bootstrap seed, and 95% confidence intervals;
- repeated-judge standard deviation;
- run ID, session mode, and the number of contaminated cases;
- case-level responses, scores, session IDs, pre-run history lengths, and deterministic
  `hard_failures`.

### Session isolation

Every case runs under its own `eval-{run_id}-{case_id}` session, and its
conversation history is cleared before and after the case. This matters because
`recommend()` loads the session history first: with a shared session, a later case
can be classified as a follow-up question, which **skips retrieval entirely** and
answers from the *previous case's* products
(`recommend_service._handle_followup`). Disabling the response cache does not
prevent this — conversation history lives in a separate Redis key with its own TTL
(default 2h), so re-running an evaluation within that window could also inherit the
previous run's history.

Reports expose `metrics.contaminated_cases` — the number of cases that started with
non-empty history. It must be `0` for an isolated run.

`--session-mode shared` reproduces the pre-isolation behavior. Use it only to
measure the impact of contamination, never to produce a baseline:

```bash
# Contamination impact (A/B on the same dataset and judge)
python eval/run_response_eval.py --session-mode shared    --out eval/results/shared.json
python eval/run_response_eval.py --session-mode isolated  --out eval/results/isolated.json
```

Multi-turn scenarios will need turns to share one session on purpose; that is a
separate mode from the per-case isolation described here.

### Prompt under evaluation

`--gen-prompt` defaults to `settings.gen_prompt_name`, i.e. the prompt production
actually serves. A baseline measured with any other prompt does not describe the
service. Reports record `service_gen_prompt`, `service_gen_prompt_version`, and
`matches_service_prompt`; the summary prints a warning when they diverge. Pass
`--gen-prompt` explicitly only to compare against an older version.

### Changing the rubric

Editing `response_judge.txt` changes what the scores mean, so `judge_prompt_version`
is recorded in every report and old rubrics are kept as `response_judge.v1.txt` etc.

Do not measure a rubric change by re-running the whole evaluation: vLLM is not fully
deterministic even at temperature 0, so the responses change too and the score delta
mixes both effects. Re-score the **stored** responses instead:

```bash
python eval/rejudge.py --report eval/results/<report>.json \
  --judge-prompt response_judge --judge-repeats 3 --out eval/results/<report>-rejudged.json

python eval/rejudge.py --compare eval/results/<report>.json eval/results/<report>-rejudged.json
```

`rejudge.py` needs no GPU and no Neo4j — it reuses each case's saved `response` and
`evidence`, so the only thing that varies is the rubric. `--compare` prints the
per-dimension delta and every case whose score moved.

### Deterministic checks

Not everything belongs in the rubric. `eval/hard_checks.py` checks the final
user-visible result without an LLM. The response gate requires
`hard_failure_rate == 0`; each failed case stores a machine-readable code and detail.

Current hard failures are:

- empty response or Hanja leakage;
- a product returned for an unverified product-level constraint;
- an unknown product or an ingredient attributed to a product without matching evidence;
- a product whose target concern conflicts with the request;
- duplicated brand names and known non-consumer terms such as `심부`.

Runtime guard activation is telemetry, not a hard failure. The checker runs after
those guards and fails only when an invalid result still reaches the user. Hanja
compatibility metrics (`hanja_leak_cases` / `hanja_leak_rate`) remain in the report,
but the CI rule is centralized on `hard_failure_rate`.

This was a measured decision, not a preference. Adding a "penalize Chinese characters"
clause to `korean_quality` moved scores the wrong way: the three cases that actually
leaked went **up** (+0.33) while clean cases went **down** (−0.16). The judge does not
detect it reliably; a regex does, exactly. Prefer a deterministic check whenever the
property is mechanically decidable.

## Judge validation

A judge score is not evidence until the judge itself has been checked. Two
independent checks are supported.

The primary dimensions are `concern_fit`, `grounding`, and `korean_quality`.
`conciseness` and `format_adherence` are secondary diagnostics because their human
rubrics were substantially more subjective. In the 2026-09-22 blind calibration of
`gpt-4o-mini` with rubric `2e1ea732`, the 40-case primary composite had MAE `0.753`,
Pearson `0.4835`, and Spearman `0.4615`; the judge over-scored it by `0.753` on average.
This is useful directional signal, but not a calibrated absolute release score.
Semantic scores are therefore observability-only. The response gate currently uses
deterministic error, Hanja leakage, and session-contamination rates. See
`P0_VALIDATION.md` for the current status.

**Judge self-consistency** — `--judge-repeats 3` scores each response three times
and reports `judge_repeat_stddev`. This is the noise floor: score differences
smaller than it must not be read as regressions.

**Judge vs. human** — collect blind human labels first, then calibrate:

```bash
# 1. Label 40 sampled responses without seeing judge scores.
python eval/label_responses.py \
  --report eval/results/<report>.json --labeler <name> --sample 40

# 2. (Optional) A second labeler on the same sample — same --sample and --seed.
python eval/label_responses.py \
  --report eval/results/<report>.json --labeler <other> --sample 40

# 3. Inter-labeler agreement — the ceiling any judge can realistically reach.
python eval/label_responses.py \
  --agreement eval/labels/<name>.jsonl eval/labels/<other>.jsonl

# 4. Judge vs. human. Compare against the exact stored responses that were labeled;
#    do not generate a new run whose wording may differ.
python eval/label_responses.py \
  --calibrate eval/results/<report>.json eval/labels/<name>.jsonl \
  --calibration-out eval/results/<report>-human-calibration.json
```

The calibrated output includes judge-vs-human MAE, Pearson correlation, and
Spearman correlation globally and per rubric dimension.

`label_responses.py` never prints judge scores or comments — labeling against a
visible model score inflates agreement. It shows the same evidence context and the
same rubric (extracted from `response_judge.txt`) that the judge receives, samples
without reference to judge scores, shuffles presentation order, and appends after
each case so an interrupted session resumes with the same command. Human label
format and review rules are documented in `LABELING.md`.

Reports store each case's `evidence` — the rendered ingredient and product context
handed to the judge. Labelers need it to score `grounding` at all; reports produced
before this field existed can still be labeled, but grounding is not assessable
from them.

## Multi-turn and transport parity

`multiturn_dataset.jsonl` contains 15 scenarios covering new requests, follow-ups,
deictic selection, topic changes, and missing history. Run each scenario through
both the batch and SSE paths:

```bash
GEN_TEMPERATURE=0 RECOMMEND_CACHE_ENABLED=false PYTHONPATH=. \
python eval/run_multiturn_eval.py --transport both \
  --out eval/results/multiturn-v7.json
```

The report records the code SHA, dataset hash, model, and production prompt version.
It fails when a follow-up changes the previous product set, the missing-history
contract breaks, Hanja leaks, a request errors, or batch/SSE return different product
sets. See `P0_VALIDATION.md` for the fixed human sample and P0 exit criteria.

Retrieval reports keep the overall `product_zero_rate` for observability, but the
release gate uses `unexpected_product_zero_rate`. Cases with no product concern, or
with product-level constraints that the current data cannot verify, are intentional
refusals and are excluded from that denominator.
