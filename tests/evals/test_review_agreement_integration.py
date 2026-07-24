"""Integration tests for evals.review_agreement — require inspect-ai.

Skipped when inspect-ai is not installed (``pip install -e ".[evals]"``).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

try:
    import inspect_ai  # noqa: F401

    _HAS_INSPECT = True
except ImportError:
    _HAS_INSPECT = False

pytestmark = pytest.mark.skipif(not _HAS_INSPECT, reason="inspect-ai not installed")


@pytest.fixture()
def golden_file(tmp_path: Path) -> Path:
    """Write a minimal golden JSONL file for task construction."""
    samples = [
        {
            "task_id": "t1",
            "run_id": 1,
            "ac_text": "Tests pass",
            "worker_summary": "All green",
            "verdict_label": "APPROVED",
        },
        {
            "task_id": "t2",
            "run_id": 2,
            "ac_text": "Lint clean",
            "worker_summary": "3 errors remain",
            "verdict_label": "REQUEST_CHANGES",
        },
    ]
    p = tmp_path / "golden.jsonl"
    p.write_text("\n".join(json.dumps(s) for s in samples) + "\n")
    return p


class TestTaskDiscovery:
    """Finding 2: @task must be at module top level for AST discovery."""

    def test_load_tasks_finds_review_agreement(self) -> None:
        from inspect_ai._eval.loader import load_tasks

        tasks = load_tasks(["evals/review_agreement.py"])
        names = [t.name for t in tasks]
        assert "review_agreement" in names, (
            f"review_agreement not discovered; found: {names}"
        )

    def test_task_decorator_is_top_level(self) -> None:
        """AST check: @task decorator must be on a top-level FunctionDef."""
        import ast

        source = Path("evals/review_agreement.py").read_text()
        tree = ast.parse(source)
        top_level_task_funcs = [
            node.name
            for node in ast.iter_child_nodes(tree)
            if isinstance(node, ast.FunctionDef)
            and any(
                (isinstance(d, ast.Name) and d.id == "task")
                or (isinstance(d, ast.Call) and isinstance(d.func, ast.Name) and d.func.id == "task")
                for d in node.decorator_list
            )
        ]
        assert top_level_task_funcs, "No top-level @task function found"


class TestMetricRegistration:
    """Finding 1: confusion-matrix metric must use @metric decorator."""

    def test_confusion_matrix_is_registered_metric(self) -> None:
        from inspect_ai.scorer._metric import metric_create

        # If @metric was applied, metric_create can instantiate by name
        m = metric_create("_label_confusion")
        assert callable(m)

    def test_confusion_matrix_computes(self) -> None:
        from evals.review_agreement import _label_confusion
        from inspect_ai.scorer import Score
        from inspect_ai.scorer._metric import SampleScore

        metric_fn = _label_confusion()
        scores = [
            SampleScore(score=Score(value="C", answer="APPROVED", metadata={"predicted": "APPROVED", "expected": "APPROVED"}), sample_id="1"),
            SampleScore(score=Score(value="I", answer="APPROVED", metadata={"predicted": "APPROVED", "expected": "REQUEST_CHANGES"}), sample_id="2"),
            SampleScore(score=Score(value="C", answer="REQUEST_CHANGES", metadata={"predicted": "REQUEST_CHANGES", "expected": "REQUEST_CHANGES"}), sample_id="3"),
            SampleScore(score=Score(value="I", answer="REQUEST_CHANGES", metadata={"predicted": "REQUEST_CHANGES", "expected": "APPROVED"}), sample_id="4"),
        ]
        result = metric_fn(scores)
        assert result == {"tp": 1, "fp": 1, "tn": 1, "fn": 1}


class TestTaskConstruction:
    """Task builds correctly from a golden file."""

    def test_review_agreement_builds_task(self, golden_file: Path) -> None:
        from evals.review_agreement import review_agreement

        t = review_agreement(golden_path=str(golden_file))
        assert t is not None
        assert len(t.dataset) == 2

    def test_task_has_scorer_and_solver(self, golden_file: Path) -> None:
        from evals.review_agreement import review_agreement

        t = review_agreement(golden_path=str(golden_file))
        assert t.scorer is not None
        assert t.solver is not None