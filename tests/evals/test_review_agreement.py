"""Tests for evals.review_agreement — pure logic, no inspect-ai needed."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.review_agreement import (
    normalize_label,
    labels_match,
    load_samples,
    PROMPT_TEMPLATE,
)


class TestNormalizeLabel:
    def test_exact_approved(self) -> None:
        assert normalize_label("APPROVED") == "APPROVED"

    def test_exact_request_changes(self) -> None:
        assert normalize_label("REQUEST_CHANGES") == "REQUEST_CHANGES"

    def test_lowercase(self) -> None:
        assert normalize_label("approved") == "APPROVED"

    def test_whitespace(self) -> None:
        assert normalize_label("  APPROVED  ") == "APPROVED"

    def test_space_variant(self) -> None:
        assert normalize_label("REQUEST CHANGES") == "REQUEST_CHANGES"

    def test_embedded_in_sentence(self) -> None:
        assert normalize_label("My verdict is APPROVED.") == "APPROVED"

    def test_ambiguous_returns_upper(self) -> None:
        result = normalize_label("I think maybe")
        assert result == "I THINK MAYBE"

    def test_both_labels_present(self) -> None:
        # Both labels in text → ambiguous, returns upper
        result = normalize_label("APPROVED or REQUEST_CHANGES")
        assert result not in ("APPROVED", "REQUEST_CHANGES")


class TestLabelsMatch:
    def test_exact(self) -> None:
        assert labels_match("APPROVED", "APPROVED") is True

    def test_case_insensitive(self) -> None:
        assert labels_match("approved", "APPROVED") is True

    def test_mismatch(self) -> None:
        assert labels_match("APPROVED", "REQUEST_CHANGES") is False

    def test_space_variant(self) -> None:
        assert labels_match("request changes", "REQUEST_CHANGES") is True


class TestLoadSamples:
    def test_roundtrip(self, tmp_path: Path) -> None:
        samples = [
            {"task_id": "t1", "run_id": 1, "ac_text": "AC", "worker_summary": "ok", "verdict_label": "APPROVED"},
            {"task_id": "t2", "run_id": 2, "ac_text": "AC2", "worker_summary": "fix", "verdict_label": "REQUEST_CHANGES"},
        ]
        p = tmp_path / "golden.jsonl"
        p.write_text("\n".join(json.dumps(s) for s in samples) + "\n")
        loaded = load_samples(p)
        assert len(loaded) == 2
        assert loaded[0]["verdict_label"] == "APPROVED"

    def test_skips_blank_lines(self, tmp_path: Path) -> None:
        p = tmp_path / "golden.jsonl"
        p.write_text('{"task_id":"t1","run_id":1,"ac_text":"","worker_summary":"x","verdict_label":"APPROVED"}\n\n')
        assert len(load_samples(p)) == 1


class TestPromptTemplate:
    def test_contains_placeholders(self) -> None:
        assert "{ac_text}" in PROMPT_TEMPLATE
        assert "{worker_summary}" in PROMPT_TEMPLATE

    def test_format(self) -> None:
        result = PROMPT_TEMPLATE.format(ac_text="AC-1", worker_summary="Done")
        assert "AC-1" in result
        assert "Done" in result
        assert "APPROVED" in result  # instructions mention labels
