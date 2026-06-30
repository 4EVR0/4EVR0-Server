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
- case-level responses and scores.

To calibrate the external judge against independently labeled expert scores:

```bash
python eval/run_response_eval.py \
  --human-labels eval/human_labels.jsonl \
  --out eval/results/calibrated.json
```

The output includes judge-vs-human MAE, Pearson correlation, and Spearman
correlation globally and per rubric dimension. Human label format and review rules
are documented in `LABELING.md`.
