"""Push eval scores to Langfuse via POST /api/public/scores.

Uses deterministic score IDs (``inspect-<EVALRUNID>-<SAMPLEID>``) for
upsert idempotency.  Env keys match the observability plugin:
``HERMES_LANGFUSE_BASE_URL``, ``HERMES_LANGFUSE_PUBLIC_KEY``,
``HERMES_LANGFUSE_SECRET_KEY``.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class SampleScore:
    """Per-sample eval outcome."""

    sample_id: str
    value: float  # 0.0 or 1.0
    predicted: str = ""
    expected: str = ""


@dataclass
class EvalRunResult:
    """Aggregated result of one eval run."""

    eval_run_id: str
    model: str
    accuracy: float
    golden_set_size: int
    samples: list[SampleScore] = field(default_factory=list)


def push_scores(
    result: EvalRunResult,
    base_url: str | None = None,
    public_key: str | None = None,
    secret_key: str | None = None,
    *,
    push_per_sample: bool = True,
) -> dict:
    """Push eval scores to Langfuse.

    Returns ``{"pushed": <int>, "trace_id": <str>}``.
    """
    base_url = (
        base_url
        or os.environ.get("HERMES_LANGFUSE_BASE_URL", "http://localhost:3000")
    ).rstrip("/")
    public_key = public_key or os.environ.get("HERMES_LANGFUSE_PUBLIC_KEY", "")
    secret_key = secret_key or os.environ.get("HERMES_LANGFUSE_SECRET_KEY", "")

    auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/json",
    }

    trace_id = f"inspect-{result.eval_run_id}"
    meta = {"model": result.model, "golden_set_size": result.golden_set_size}
    ts = datetime.now(timezone.utc).isoformat()

    # 1. Create trace via ingestion API
    _post(
        f"{base_url}/api/public/ingestion",
        headers,
        {
            "batch": [
                {
                    "type": "trace-create",
                    "id": trace_id,
                    "timestamp": ts,
                    "body": {
                        "id": trace_id,
                        "name": "eval-review-agreement",
                        "timestamp": ts,
                        "metadata": meta,
                    },
                }
            ]
        },
    )

    pushed = 0

    # 2. Per-run accuracy score
    _post(
        f"{base_url}/api/public/scores",
        headers,
        {
            "id": f"inspect-{result.eval_run_id}-run",
            "name": "eval_review_agreement",
            "value": result.accuracy,
            "traceId": trace_id,
            "dataType": "NUMERIC",
            "metadata": meta,
        },
    )
    pushed += 1

    # 3. Per-sample 0/1 scores
    if push_per_sample:
        for s in result.samples:
            _post(
                f"{base_url}/api/public/scores",
                headers,
                {
                    "id": f"inspect-{result.eval_run_id}-{s.sample_id}",
                    "name": "eval_review_agreement",
                    "value": s.value,
                    "traceId": trace_id,
                    "dataType": "NUMERIC",
                    "comment": f"predicted={s.predicted} expected={s.expected}",
                    "metadata": meta,
                },
            )
            pushed += 1

    return {"pushed": pushed, "trace_id": trace_id}


def from_eval_log(log_path: str) -> EvalRunResult:
    """Convert an inspect-ai ``.eval`` log to an :class:`EvalRunResult`."""
    try:
        from inspect_ai.log import read_eval_log
    except ImportError:
        msg = "inspect-ai required: pip install -e '.[evals]'"
        raise ImportError(msg) from None

    log = read_eval_log(log_path)
    samples: list[SampleScore] = []
    correct = total = 0

    for sample in log.samples or []:
        if not sample.scores:
            continue
        for _name, ss in sample.scores.items():
            val = ss.value
            is_correct = val in ("C", 1, 1.0, True)
            meta = ss.metadata or {}
            samples.append(
                SampleScore(
                    sample_id=str(sample.id),
                    value=1.0 if is_correct else 0.0,
                    predicted=meta.get("predicted", ""),
                    expected=meta.get("expected", ""),
                )
            )
            total += 1
            if is_correct:
                correct += 1
            break  # one scorer per sample

    return EvalRunResult(
        eval_run_id=log.eval.eval_id,
        model=log.eval.model,
        accuracy=correct / total if total else 0.0,
        golden_set_size=total,
        samples=samples,
    )


def _post(url: str, headers: dict, body: dict) -> None:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("eval_log", help="Path to .eval log file")
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--public-key", default=None)
    ap.add_argument("--secret-key", default=None)
    args = ap.parse_args()
    result = from_eval_log(args.eval_log)
    summary = push_scores(
        result, args.base_url, args.public_key, args.secret_key
    )
    print(f"Pushed {summary['pushed']} scores (trace: {summary['trace_id']})")
