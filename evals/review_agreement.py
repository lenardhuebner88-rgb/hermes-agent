"""Inspect-ai eval task: review-agreement.

Measures whether a model can reproduce the review verdict (APPROVED or
REQUEST_CHANGES) given acceptance criteria and a worker summary.

Pure logic functions (normalize_label, labels_match) are importable
without inspect-ai installed.  The @task / @scorer definitions require
``pip install -e ".[evals]"``.
"""
from __future__ import annotations

import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Pure logic — no inspect-ai dependency
# ---------------------------------------------------------------------------

VALID_LABELS = frozenset({"APPROVED", "REQUEST_CHANGES"})

GOLDEN_PATH = (
    Path.home() / ".hermes" / "evals" / "golden" / "review_agreement.jsonl"
)

PROMPT_TEMPLATE = """\
You are a code-review judge. Given the acceptance criteria (AC) and the \
worker's completion summary, decide whether the work should be approved.

Respond with exactly one label: APPROVED or REQUEST_CHANGES

## Acceptance Criteria
{ac_text}

## Worker Summary
{worker_summary}

Your verdict:"""


def normalize_label(text: str) -> str:
    """Extract a canonical label from model output text."""
    upper = text.strip().upper().replace("REQUEST CHANGES", "REQUEST_CHANGES")
    if upper in VALID_LABELS:
        return upper
    found = [lbl for lbl in sorted(VALID_LABELS) if lbl in upper]
    if len(found) == 1:
        return found[0]
    return upper  # ambiguous / unrecognised


def labels_match(predicted: str, expected: str) -> bool:
    """True when both strings normalise to the same canonical label."""
    return normalize_label(predicted) == normalize_label(expected)


def load_samples(golden_path: str | Path = GOLDEN_PATH) -> list[dict]:
    """Load golden samples from a JSONL file."""
    path = Path(golden_path).expanduser()
    out: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


# ---------------------------------------------------------------------------
# inspect-ai integration (requires: pip install -e ".[evals]")
# ---------------------------------------------------------------------------

try:
    from inspect_ai import Task, task
    from inspect_ai.dataset import MemoryDataset, Sample
    from inspect_ai.scorer import Score, Target, accuracy, metric, scorer
    from inspect_ai.solver import generate

    _HAS_INSPECT = True
except ImportError:  # pragma: no cover
    _HAS_INSPECT = False

    # No-op stubs keep @task / @scorer / @metric syntactically present at
    # module top level so inspect-ai's AST-based task discovery
    # (code_has_decorator → ast.iter_child_nodes) finds the @task function,
    # while pure-logic imports still work without inspect-ai installed.
    def task(func):  # type: ignore[no-redef]  # noqa: ANN001, ANN202
        return func

    def scorer(*_a, **_kw):  # type: ignore[no-redef]  # noqa: ANN001, ANN202
        def _wrap(f):  # noqa: ANN001, ANN202
            return f
        return _wrap

    def metric(func):  # type: ignore[no-redef]  # noqa: ANN001, ANN202
        return func

    def accuracy(*_a, **_kw):  # type: ignore[no-redef]  # noqa: ANN001, ANN202
        return None

    Task = None  # type: ignore[assignment]
    MemoryDataset = None  # type: ignore[assignment]
    Sample = None  # type: ignore[assignment]
    Score = None  # type: ignore[assignment]
    Target = None  # type: ignore[assignment]
    generate = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Metric, scorer, and task — top-level so inspect-ai's AST-based discovery
# (ast.iter_child_nodes on the module tree) finds the @task decorator.
# ---------------------------------------------------------------------------


@metric
def _label_confusion():  # noqa: ANN202
    """Confusion-matrix metric (APPROVED as positive class)."""

    def compute(scores):  # noqa: ANN001, ANN202
        tp = fp = tn = fn = 0
        for s in scores:
            # metrics receive SampleScore wrappers; the actual Score is .score
            score_obj = getattr(s, "score", s)
            meta = score_obj.metadata or {}
            pred = meta.get("predicted", "")
            exp = meta.get("expected", "")
            if exp == "APPROVED":
                if pred == "APPROVED":
                    tp += 1
                else:
                    fn += 1
            elif exp == "REQUEST_CHANGES":
                if pred == "APPROVED":
                    fp += 1
                else:
                    tn += 1
        return {"tp": tp, "fp": fp, "tn": tn, "fn": fn}

    return compute


@scorer(metrics=[accuracy(), _label_confusion()])
def review_scorer():  # noqa: ANN202
    """Deterministic exact-match scorer after label normalisation."""

    async def score(state, target):  # noqa: ANN001, ANN202
        output = state.output.completion if state.output else ""
        predicted = normalize_label(output)
        expected = normalize_label(target.text)
        correct = predicted == expected
        return Score(
            value="C" if correct else "I",
            answer=predicted,
            explanation=f"predicted={predicted} expected={expected}",
            metadata={"predicted": predicted, "expected": expected},
        )

    return score


@task
def review_agreement(
    golden_path: str = str(GOLDEN_PATH),
) -> Task:
    """Review-agreement eval: can the model reproduce review verdicts?"""
    raw = load_samples(golden_path)
    samples = [
        Sample(
            input=PROMPT_TEMPLATE.format(
                ac_text=s["ac_text"],
                worker_summary=s["worker_summary"],
            ),
            target=s["verdict_label"],
            id=f"{s['task_id']}-{s['run_id']}",
            metadata={"task_id": s["task_id"], "run_id": s["run_id"]},
        )
        for s in raw
    ]
    return Task(
        dataset=MemoryDataset(samples=samples, name="review_agreement"),
        solver=generate(),
        scorer=review_scorer(),
    )