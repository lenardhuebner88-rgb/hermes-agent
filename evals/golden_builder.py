"""Build a golden evaluation set from kanban review verdicts.

Reads the kanban database strictly read-only (sqlite3 file-URI, mode=ro),
samples completed task_runs with a review verdict and non-empty summary,
joins the task body (acceptance criteria), and writes a balanced JSONL
golden set to ~/.hermes/evals/golden/review_agreement.jsonl.
"""
from __future__ import annotations

import json
import random
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path.home() / ".hermes" / "kanban.db"
DEFAULT_OUTPUT_PATH = (
    Path.home() / ".hermes" / "evals" / "golden" / "review_agreement.jsonl"
)
MAX_SKEW = 0.70  # maximum allowed label imbalance (70/30)

_QUERY = """\
SELECT
    tr.id   AS run_id,
    tr.task_id,
    tr.verdict,
    tr.summary,
    t.body
FROM task_runs tr
JOIN tasks t ON t.id = tr.task_id
WHERE tr.verdict IN ('APPROVED', 'REQUEST_CHANGES')
  AND tr.summary IS NOT NULL
  AND TRIM(tr.summary) != ''
ORDER BY tr.id
"""


def build_golden_set(
    db_path: str | Path = DEFAULT_DB_PATH,
    output_path: str | Path = DEFAULT_OUTPUT_PATH,
    max_skew: float = MAX_SKEW,
    seed: int = 42,
) -> Path:
    """Build a balanced golden set and write it as JSONL.

    Returns the output path.
    """
    db_path = Path(db_path)
    output_path = Path(output_path)

    # Strictly read-only via file-URI (never sqlite3 CLI)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(_QUERY).fetchall()
    finally:
        conn.close()

    by_verdict: dict[str, list[dict]] = {}
    for row in rows:
        sample = {
            "task_id": row["task_id"],
            "run_id": row["run_id"],
            "ac_text": row["body"] or "",
            "worker_summary": row["summary"],
            "verdict_label": row["verdict"],
        }
        by_verdict.setdefault(row["verdict"], []).append(sample)

    samples = _balance(by_verdict, max_skew, seed)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as fh:
        for s in samples:
            fh.write(json.dumps(s, ensure_ascii=False) + "\n")

    return output_path


def _balance(
    by_verdict: dict[str, list[dict]],
    max_skew: float,
    seed: int,
) -> list[dict]:
    """Downsample the majority label to at most *max_skew* fraction."""
    if not by_verdict:
        return []
    labels = sorted(by_verdict)
    if len(labels) < 2:
        return [s for lbl in labels for s in by_verdict[lbl]]

    counts = {lbl: len(by_verdict[lbl]) for lbl in labels}
    total = sum(counts.values())
    majority = max(counts, key=counts.get)  # type: ignore[arg-type]
    minority_count = min(counts.values())

    if counts[majority] / total > max_skew:
        target = int(minority_count * max_skew / (1.0 - max_skew))
        rng = random.Random(seed)
        by_verdict[majority] = rng.sample(by_verdict[majority], target)

    result = [s for lbl in labels for s in by_verdict[lbl]]
    random.Random(seed).shuffle(result)
    return result


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(DEFAULT_DB_PATH))
    ap.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH))
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    out = build_golden_set(args.db, args.output, seed=args.seed)
    print(f"Golden set written to {out}")
