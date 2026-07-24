"""Tests for evals.langfuse_push against a fake HTTP server."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from evals.langfuse_push import EvalRunResult, SampleScore, push_scores


class _FakeLangfuseHandler(BaseHTTPRequestHandler):
    """Captures POST bodies for assertion."""

    received: list[dict] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        _FakeLangfuseHandler.received.append(
            {"path": self.path, "body": body, "auth": self.headers.get("Authorization", "")}
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # silence


@pytest.fixture()
def fake_server():
    _FakeLangfuseHandler.received = []
    server = HTTPServer(("127.0.0.1", 0), _FakeLangfuseHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


def _make_result() -> EvalRunResult:
    return EvalRunResult(
        eval_run_id="run-42",
        model="test-model",
        accuracy=0.75,
        golden_set_size=4,
        samples=[
            SampleScore("t1-1", 1.0, "APPROVED", "APPROVED"),
            SampleScore("t2-2", 0.0, "APPROVED", "REQUEST_CHANGES"),
            SampleScore("t3-3", 1.0, "REQUEST_CHANGES", "REQUEST_CHANGES"),
            SampleScore("t4-4", 1.0, "APPROVED", "APPROVED"),
        ],
    )


class TestPushScores:
    def test_pushes_run_and_sample_scores(self, fake_server: str) -> None:
        result = _make_result()
        summary = push_scores(
            result,
            base_url=fake_server,
            public_key="pk-lf-test",
            secret_key="sk-lf-test",
        )
        # 1 trace + 1 run score + 4 sample scores = 6 requests
        assert summary["pushed"] == 5  # 1 run + 4 samples
        assert summary["trace_id"] == "inspect-run-42"
        # trace creation + 5 score posts
        assert len(_FakeLangfuseHandler.received) == 6

    def test_deterministic_score_ids(self, fake_server: str) -> None:
        result = _make_result()
        push_scores(result, base_url=fake_server, public_key="pk", secret_key="sk")
        score_posts = [
            r for r in _FakeLangfuseHandler.received if r["path"] == "/api/public/scores"
        ]
        ids = [r["body"]["id"] for r in score_posts]
        assert "inspect-run-42-run" in ids
        assert "inspect-run-42-t1-1" in ids
        assert "inspect-run-42-t4-4" in ids

    def test_score_name(self, fake_server: str) -> None:
        result = _make_result()
        push_scores(result, base_url=fake_server, public_key="pk", secret_key="sk")
        for r in _FakeLangfuseHandler.received:
            if r["path"] == "/api/public/scores":
                assert r["body"]["name"] == "eval_review_agreement"

    def test_metadata_carries_model_and_size(self, fake_server: str) -> None:
        result = _make_result()
        push_scores(result, base_url=fake_server, public_key="pk", secret_key="sk")
        for r in _FakeLangfuseHandler.received:
            if r["path"] == "/api/public/scores":
                meta = r["body"]["metadata"]
                assert meta["model"] == "test-model"
                assert meta["golden_set_size"] == 4

    def test_basic_auth_header(self, fake_server: str) -> None:
        result = _make_result()
        push_scores(result, base_url=fake_server, public_key="pk-lf-x", secret_key="sk-lf-y")
        for r in _FakeLangfuseHandler.received:
            assert r["auth"].startswith("Basic ")

    def test_no_per_sample(self, fake_server: str) -> None:
        result = _make_result()
        summary = push_scores(
            result, base_url=fake_server, public_key="pk", secret_key="sk",
            push_per_sample=False,
        )
        assert summary["pushed"] == 1  # only run-level score

    def test_run_score_value_is_accuracy(self, fake_server: str) -> None:
        result = _make_result()
        push_scores(result, base_url=fake_server, public_key="pk", secret_key="sk")
        run_score = next(
            r for r in _FakeLangfuseHandler.received
            if r["path"] == "/api/public/scores" and r["body"]["id"] == "inspect-run-42-run"
        )
        assert run_score["body"]["value"] == 0.75

    def test_idempotent_ids(self, fake_server: str) -> None:
        """Same input → same IDs (upsert-safe)."""
        result = _make_result()
        push_scores(result, base_url=fake_server, public_key="pk", secret_key="sk")
        ids_first = [
            r["body"]["id"] for r in _FakeLangfuseHandler.received
            if r["path"] == "/api/public/scores"
        ]
        _FakeLangfuseHandler.received = []
        push_scores(result, base_url=fake_server, public_key="pk", secret_key="sk")
        ids_second = [
            r["body"]["id"] for r in _FakeLangfuseHandler.received
            if r["path"] == "/api/public/scores"
        ]
        assert ids_first == ids_second
