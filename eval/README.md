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
- case-level responses, scores, session IDs, and pre-run history lengths.

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

## Judge validation

A judge score is not evidence until the judge itself has been checked. Two
independent checks are supported.

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

# 4. Judge vs. human.
python eval/run_response_eval.py \
  --human-labels eval/labels/<name>.jsonl \
  --out eval/results/calibrated.json
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
