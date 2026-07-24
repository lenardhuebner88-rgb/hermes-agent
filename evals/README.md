# Hermes Evals

Evaluation harness for Hermes Agent — golden-set builders, inspect-ai
tasks, and Langfuse score pushers.

## Install

```bash
pip install -e ".[evals]"
```

Without the `evals` extra the runtime path is unchanged — nothing in
`hermes_cli/` or `agent/` imports from `evals/`.

## 1. Build the golden set

```bash
python -m evals.golden_builder
# → ~/.hermes/evals/golden/review_agreement.jsonl
```

Reads `~/.hermes/kanban.db` strictly read-only (sqlite3 file-URI,
`mode=ro`).  Samples completed `task_runs` with a review verdict
(`APPROVED` / `REQUEST_CHANGES`) and non-empty summary, joins the task
body (acceptance criteria), and writes one JSON object per line:

```json
{"task_id": "…", "run_id": 42, "ac_text": "…", "worker_summary": "…", "verdict_label": "APPROVED"}
```

Labels are balanced to at most 70/30 skew (majority is downsampled).

## 2. Run the review-agreement eval

```bash
inspect eval evals/review_agreement.py \
  --model openai/gpt-4o-mini \
  -T golden_path=~/.hermes/evals/golden/review_agreement.jsonl
```

Model selection uses inspect-native provider env vars — no hard-coded
provider.  For an OpenAI-compatible subscription endpoint from
`~/.hermes/.env`:

```bash
source ~/.hermes/.env
export OPENAI_API_KEY="$HERMES_OPENAI_API_KEY"   # or the matching key
export OPENAI_BASE_URL="$HERMES_OPENAI_BASE_URL"  # if non-default
inspect eval evals/review_agreement.py --model openai/gpt-4o-mini
```

The solver sends AC text + worker summary and asks for exactly one
label.  The scorer is deterministic exact-match after normalisation.
Metrics: `accuracy` + label confusion matrix (tp/fp/tn/fn with
APPROVED as positive class).

## 3. Push scores to Langfuse

```bash
python -m evals.langfuse_push <path-to-.eval-log>
```

Posts scores via `POST /api/public/scores` to loopback Langfuse.
Env keys (same as the observability plugin):

| Variable | Purpose |
|---|---|
| `HERMES_LANGFUSE_BASE_URL` | Langfuse server (default `http://localhost:3000`) |
| `HERMES_LANGFUSE_PUBLIC_KEY` | Project public key (`pk-lf-…`) |
| `HERMES_LANGFUSE_SECRET_KEY` | Project secret key (`sk-lf-…`) |

Score name: `eval_review_agreement`.  One run-level accuracy score plus
optional per-sample 0/1 scores.  Deterministic IDs
(`inspect-<EVALRUNID>-<SAMPLEID>`) ensure upsert idempotency.
Metadata carries `model` and `golden_set_size`.

## Tests

```bash
scripts/run_tests.sh tests/evals/
```

All tests run without a live model or Langfuse instance.
