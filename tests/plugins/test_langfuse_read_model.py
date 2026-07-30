"""Contract tests for the sanitized Hermes Langfuse read model."""

from __future__ import annotations

import threading

from plugins.kanban.dashboard import langfuse_read_model as read_model


def setup_function() -> None:
    read_model.clear_cache()


def teardown_function() -> None:
    read_model.clear_cache()


def test_aggregate_exposes_denominators_and_never_raw_payloads():
    observations = [
        {
            "providedModelName": "model-a",
            "startTime": "2026-07-29T00:00:00Z",
            "completionStartTime": "2026-07-29T00:00:00.400Z",
            "endTime": "2026-07-29T00:00:02Z",
            "calculatedTotalCost": 0.1,
            "usageDetails": {"total": 150},
            "input": "secret prompt must not leave the server",
        },
        {
            "startTime": "2026-07-29T00:01:00Z",
            "endTime": "2026-07-29T00:01:06Z",
            "usageDetails": {"input": 30, "output": 5},
            "output": "secret answer must not leave the server",
        },
    ]

    payload = read_model._aggregate(
        observations,
        days=7,
        generated_at="2026-07-29T01:00:00Z",
    )

    assert payload["available"] is True
    assert payload["metrics"]["generation_calls"]["value"] == 2
    assert payload["metrics"]["latency_p50_ms"] == {
        "value": 2000.0,
        "denominator": 2,
        "sample_denominator": 2,
        "observed": 2,
        "coverage": 1.0,
        "sample_coverage": 1.0,
        "coverage_reason": None,
        "completeness": "complete",
        "value_semantics": "exact",
        "truncation_reason": None,
        "source": "langfuse.generations.time",
        "captured_at": "2026-07-29T00:01:06Z",
    }
    assert payload["metrics"]["ttft_p50_ms"]["coverage"] == 0.5
    assert payload["metrics"]["known_cost_usd"]["coverage"] == 0.5
    assert payload["metrics"]["tokens_total"]["value"] == 185.0
    assert payload["coverage"]["model_unknown"] == 1
    assert "secret prompt" not in repr(payload)
    assert "secret answer" not in repr(payload)


def test_remote_snapshot_selects_only_worker_generations(monkeypatch):
    def fake_all_pages(_host, _auth, path, _params, **_kwargs):
        assert path.endswith("/observations")
        return read_model._PageScan(
            rows=[
                {
                    "metadata": {
                        "kanban_task_id": "task-worker",
                        "origin": "hermes_agent",
                    },
                    "traceId": "worker",
                    "model": "worker-model",
                    "startTime": "2026-07-29T00:00:00Z",
                    "endTime": "2026-07-29T00:00:01Z",
                },
                {
                    "metadata": {"origin": "interactive"},
                    "traceId": "normal",
                    "model": "normal-model",
                    "startTime": "2026-07-29T00:00:00Z",
                    "endTime": "2026-07-29T00:00:01Z",
                },
                {
                    "metadata": {
                        "task_id": "task-backfill",
                        "correlation_source": "claude_session_id",
                        "origin": "claude_code",
                    },
                    "traceId": "backfill",
                    "model": "backfill-model",
                    "startTime": "2026-07-29T00:00:00Z",
                    "endTime": "2026-07-29T00:00:01Z",
                },
            ],
            truncated=False,
            reason=None,
            pages=1,
        )

    monkeypatch.setattr(read_model, "_all_pages", fake_all_pages)
    payload = read_model._build_remote_snapshot(
        days=7,
        env={
            "HERMES_LANGFUSE_BASE_URL": "https://langfuse.example",
            "HERMES_LANGFUSE_PUBLIC_KEY": "public",
            "HERMES_LANGFUSE_SECRET_KEY": "secret",
        },
    )

    assert payload["metrics"]["generation_calls"]["value"] == 2
    assert [row["name"] for row in payload["models"]] == [
        "backfill-model",
        "worker-model",
    ]
    assert payload["coverage"]["scan_truncated"] is False
    assert "public" not in repr(payload)
    assert "secret" not in repr(payload)


def test_missing_credentials_is_explicit_and_fail_soft():
    payload = read_model.get_langfuse_snapshot(env={})

    assert payload["available"] is False
    assert payload["state"] == "absent"
    assert payload["reason"] == "credentials_missing"
    assert payload["metrics"] == {}


def test_standard_langfuse_triplet_is_accepted(monkeypatch):
    seen_hosts: list[str] = []

    def fake_all_pages(host, _auth, _path, _params, **_kwargs):
        seen_hosts.append(host)
        return read_model._PageScan([], False, None, 1)

    monkeypatch.setattr(read_model, "_all_pages", fake_all_pages)
    payload = read_model._build_remote_snapshot(
        days=7,
        env={
            "LANGFUSE_BASE_URL": "https://standard.example/",
            "LANGFUSE_PUBLIC_KEY": "public-standard",
            "LANGFUSE_SECRET_KEY": "secret-standard",
        },
    )

    assert payload["available"] is True
    assert seen_hosts == ["https://standard.example"]
    assert "public-standard" not in repr(payload)
    assert "secret-standard" not in repr(payload)


def test_hermes_triplet_wins_over_complete_standard_triplet(monkeypatch):
    seen_hosts: list[str] = []

    def fake_all_pages(host, _auth, _path, _params, **_kwargs):
        seen_hosts.append(host)
        return read_model._PageScan([], False, None, 1)

    monkeypatch.setattr(read_model, "_all_pages", fake_all_pages)
    payload = read_model._build_remote_snapshot(
        days=7,
        env={
            "HERMES_LANGFUSE_HOST": "https://hermes.example",
            "HERMES_LANGFUSE_PUBLIC_KEY": "public-hermes",
            "HERMES_LANGFUSE_SECRET_KEY": "secret-hermes",
            "LANGFUSE_BASE_URL": "https://standard.example",
            "LANGFUSE_PUBLIC_KEY": "public-standard",
            "LANGFUSE_SECRET_KEY": "secret-standard",
        },
    )

    assert payload["available"] is True
    assert seen_hosts == ["https://hermes.example"]


def test_incomplete_hermes_triplet_does_not_mix_with_standard_values():
    payload = read_model.get_langfuse_snapshot(
        env={
            "HERMES_LANGFUSE_PUBLIC_KEY": "public-hermes",
            "LANGFUSE_BASE_URL": "https://standard.example",
            "LANGFUSE_PUBLIC_KEY": "public-standard",
            "LANGFUSE_SECRET_KEY": "secret-standard",
        }
    )

    assert payload["available"] is False
    assert payload["reason"] == "configuration_invalid"
    assert "public-hermes" not in repr(payload)
    assert "public-standard" not in repr(payload)


def test_invalid_langfuse_url_is_a_safe_configuration_error():
    secret = "secret-must-not-leak"
    payload = read_model.get_langfuse_snapshot(
        env={
            "LANGFUSE_BASE_URL": "not-a-url",
            "LANGFUSE_PUBLIC_KEY": "public-must-not-leak",
            "LANGFUSE_SECRET_KEY": secret,
        }
    )

    assert payload["available"] is False
    assert payload["reason"] == "configuration_invalid"
    assert secret not in repr(payload)


def test_cache_is_separated_by_resolved_fallback_credentials(monkeypatch):
    calls: list[str] = []

    def fake_build(*, days, env, resolved=None):
        assert resolved is not None
        calls.append(resolved.host)
        return read_model._absent_payload(
            days=days,
            state="absent",
            reason="test",
            generated_at="2026-07-29T00:00:00Z",
        )

    monkeypatch.setattr(read_model, "_build_remote_snapshot", fake_build)
    base = {
        "LANGFUSE_BASE_URL": "https://standard.example",
        "LANGFUSE_PUBLIC_KEY": "public-standard",
    }
    read_model.get_langfuse_snapshot(
        env={**base, "LANGFUSE_SECRET_KEY": "secret-one"}
    )
    read_model.get_langfuse_snapshot(
        env={**base, "LANGFUSE_SECRET_KEY": "secret-two"}
    )
    read_model.get_langfuse_snapshot(
        env={
            "HERMES_LANGFUSE_BASE_URL": "https://hermes.example",
            "HERMES_LANGFUSE_PUBLIC_KEY": "public-hermes",
            "HERMES_LANGFUSE_SECRET_KEY": "secret-hermes",
            **base,
            "LANGFUSE_SECRET_KEY": "secret-two",
        }
    )

    assert calls == [
        "https://standard.example",
        "https://standard.example",
        "https://hermes.example",
    ]


def test_negative_cache_is_bounded_to_the_credential_identity(monkeypatch):
    calls = []

    def build(*, days, env, resolved=None):
        del resolved
        calls.append(dict(env))
        return read_model._absent_payload(
            days=days,
            state="absent",
            reason="synthetic",
            generated_at="2026-07-29T01:00:00Z",
        )

    monkeypatch.setattr(read_model, "_build_remote_snapshot", build)
    read_model.get_langfuse_snapshot(env={})
    read_model.get_langfuse_snapshot(env={})
    read_model.get_langfuse_snapshot(
        env={
            "HERMES_LANGFUSE_BASE_URL": "https://other.example",
            "HERMES_LANGFUSE_PUBLIC_KEY": "public",
            "HERMES_LANGFUSE_SECRET_KEY": "rotated",
        }
    )

    assert len(calls) == 2


def test_remote_failure_is_negative_cached(monkeypatch):
    calls = 0

    def fail(**_kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("offline")

    monkeypatch.setattr(read_model, "_build_remote_snapshot", fail)

    first = read_model.get_langfuse_snapshot(env={"project": "one"})
    second = read_model.get_langfuse_snapshot(env={"project": "one"})

    assert first["state"] == "absent"
    assert second["state"] == "absent"
    assert first["reason"] == "read_failed:RuntimeError"
    assert calls == 1


def test_refresh_disabled_never_performs_remote_io(monkeypatch):
    def forbidden(**_kwargs):
        raise AssertionError("remote refresh must not run")

    monkeypatch.setattr(read_model, "_build_remote_snapshot", forbidden)

    payload = read_model.get_langfuse_snapshot(
        env={"project": "scorecard"},
        allow_refresh=False,
    )

    assert payload["state"] == "unknown"
    assert payload["reason"] == "refresh_disabled"


def test_page_limit_degrades_to_partial_scan(monkeypatch):
    calls = 0

    def full_page(_url, _authorization, *, timeout):
        del timeout
        nonlocal calls
        calls += 1
        return {"data": [{"id": calls}] * read_model._PAGE_SIZE}

    monkeypatch.setattr(read_model, "_request_json", full_page)
    monkeypatch.setattr(read_model, "_MAX_PAGES", 2)

    scan = read_model._all_pages(
        "https://langfuse.example",
        "Basic redacted",
        "/api/public/observations",
        {},
        deadline_monotonic=read_model.time.monotonic() + 10,
    )

    assert scan.truncated is True
    assert scan.reason == "page_limit"
    assert scan.pages == 2
    assert len(scan.rows) == 2 * read_model._PAGE_SIZE


def test_truncated_snapshot_never_claims_full_window_coverage(monkeypatch):
    row = {
        "metadata": {"kanban_task_id": "worker"},
        "model": "model-a",
        "startTime": "2026-07-29T00:00:00Z",
        "completionStartTime": "2026-07-29T00:00:00.100Z",
        "endTime": "2026-07-29T00:00:01Z",
        "calculatedTotalCost": 0.25,
        "usageDetails": {"total": 10},
    }

    monkeypatch.setattr(
        read_model,
        "_all_pages",
        lambda *_args, **_kwargs: read_model._PageScan(
            rows=[row], truncated=True, reason="page_limit", pages=20
        ),
    )
    payload = read_model._build_remote_snapshot(
        days=7,
        env={
            "HERMES_LANGFUSE_BASE_URL": "https://langfuse.example",
            "HERMES_LANGFUSE_PUBLIC_KEY": "public",
            "HERMES_LANGFUSE_SECRET_KEY": "secret",
        },
    )

    assert payload["state"] == "partial"
    assert payload["reason"] == "scan_page_limit"
    for metric in payload["metrics"].values():
        assert metric["sample_denominator"] == 1
        assert metric["sample_coverage"] == 1.0
        assert metric["denominator"] is None
        assert metric["coverage"] is None
        assert metric["coverage_reason"] == "scan_page_limit"
        assert metric["truncation_reason"] == "scan_page_limit"
        assert metric["completeness"] == "partial"
    assert payload["metrics"]["generation_calls"]["value_semantics"] == "lower_bound"
    assert payload["metrics"]["known_cost_usd"]["value_semantics"] == "lower_bound"
    assert payload["coverage"]["worker_traces_semantics"] == "lower_bound"
    assert payload["models"][0]["completeness"] == "partial"
    assert payload["models"][0]["sample_denominator"] == 1
    assert payload["models"][0]["truncation_reason"] == "scan_page_limit"
    assert payload["models"][0]["cost_coverage"] is None
    assert payload["models"][0]["sample_cost_coverage"] == 1.0
    assert payload["models"][0]["latency_coverage"] is None
    assert payload["models"][0]["sample_latency_coverage"] == 1.0
    assert payload["models"][0]["coverage_reason"] == "scan_page_limit"
    assert payload["models"][0]["known_cost_semantics"] == "lower_bound"


def test_deadline_after_page_response_preserves_rows_but_marks_scan_partial(
    monkeypatch,
):
    moments = iter((10.0, 14.1))
    monkeypatch.setattr(read_model.time, "monotonic", lambda: next(moments))
    monkeypatch.setattr(
        read_model,
        "_request_json",
        lambda *_args, **_kwargs: {
            "data": [{"id": "observed"}],
            "meta": {"totalPages": 1},
        },
    )

    scan = read_model._all_pages(
        "https://langfuse.example",
        "Basic redacted",
        "/api/public/observations",
        {},
        deadline_monotonic=14.0,
    )

    assert scan.rows == [{"id": "observed"}]
    assert scan.pages == 1
    assert scan.truncated is True
    assert scan.reason == "deadline"


def test_unknown_total_pages_paginates_until_short_page(monkeypatch):
    pages = iter(
        (
            {"data": [{"id": "first"}] * read_model._PAGE_SIZE, "meta": {}},
            {"data": [{"id": "last"}], "meta": {}},
        )
    )
    monkeypatch.setattr(
        read_model, "_request_json", lambda *_args, **_kwargs: next(pages)
    )

    scan = read_model._all_pages(
        "https://langfuse.example",
        "Basic redacted",
        "/api/public/observations",
        {},
        deadline_monotonic=read_model.time.monotonic() + 10,
    )

    assert scan.truncated is False
    assert scan.pages == 2
    assert len(scan.rows) == read_model._PAGE_SIZE + 1


def test_known_total_pages_finishes_without_an_extra_request(monkeypatch):
    requested = 0

    def page(_url, _authorization, **_kwargs):
        nonlocal requested
        requested += 1
        return {
            "data": [{"id": requested}] * read_model._PAGE_SIZE,
            "meta": {"totalPages": 2},
        }

    monkeypatch.setattr(read_model, "_request_json", page)
    scan = read_model._all_pages(
        "https://langfuse.example",
        "Basic redacted",
        "/api/public/observations",
        {},
        deadline_monotonic=read_model.time.monotonic() + 10,
    )

    assert scan.truncated is False
    assert scan.pages == 2
    assert requested == 2


def test_empty_complete_scan_has_exact_zero_calls_but_unknown_sums(monkeypatch):
    monkeypatch.setattr(
        read_model,
        "_all_pages",
        lambda *_args, **_kwargs: read_model._PageScan(
            rows=[], truncated=False, reason=None, pages=1
        ),
    )
    payload = read_model._build_remote_snapshot(
        days=7,
        env={
            "HERMES_LANGFUSE_BASE_URL": "https://langfuse.example",
            "HERMES_LANGFUSE_PUBLIC_KEY": "public",
            "HERMES_LANGFUSE_SECRET_KEY": "secret",
        },
    )

    calls = payload["metrics"]["generation_calls"]
    assert calls["value"] == 0
    assert calls["completeness"] == "complete"
    assert calls["denominator"] == 0
    assert payload["metrics"]["known_cost_usd"]["value"] is None
    assert payload["metrics"]["known_cost_usd"]["completeness"] == "unknown"
    assert payload["metrics"]["tokens_total"]["value"] is None
    assert payload["metrics"]["tokens_total"]["completeness"] == "unknown"


def test_missing_cost_and_tokens_remain_null_with_observed_denominator():
    payload = read_model._aggregate(
        [{"model": "model-a"}],
        days=7,
        generated_at="2026-07-29T01:00:00Z",
    )

    for name in ("known_cost_usd", "tokens_total"):
        metric = payload["metrics"][name]
        assert metric["value"] is None
        assert metric["sample_denominator"] == 1
        assert metric["observed"] == 0
        assert metric["sample_coverage"] == 0.0
        assert metric["coverage"] == 0.0
        assert metric["completeness"] == "unknown"
        assert metric["value_semantics"] == "unknown"


def test_single_flight_waiter_ignores_foreign_cache_key_notifications(
    monkeypatch,
):
    started = threading.Event()
    release = threading.Event()
    waiter_done = threading.Event()
    waiter_payload = []
    env_a = {
        "HERMES_LANGFUSE_BASE_URL": "https://langfuse.example",
        "HERMES_LANGFUSE_PUBLIC_KEY": "project-a",
        "HERMES_LANGFUSE_SECRET_KEY": "secret-a",
    }
    env_b = {
        "HERMES_LANGFUSE_BASE_URL": "https://langfuse.example",
        "HERMES_LANGFUSE_PUBLIC_KEY": "project-b",
        "HERMES_LANGFUSE_SECRET_KEY": "secret-b",
    }

    def build(*, days, env, resolved=None):
        del resolved
        if env["HERMES_LANGFUSE_PUBLIC_KEY"] == "project-a":
            started.set()
            assert release.wait(timeout=2)
        return read_model._aggregate(
            [],
            days=days,
            generated_at="2026-07-29T01:00:00Z",
        )

    monkeypatch.setattr(read_model, "_build_remote_snapshot", build)
    leader = threading.Thread(
        target=lambda: read_model.get_langfuse_snapshot(env=env_a)
    )

    def wait_for_a():
        waiter_payload.append(
            read_model.get_langfuse_snapshot(env=env_a)
        )
        waiter_done.set()

    leader.start()
    assert started.wait(timeout=1)
    waiter = threading.Thread(target=wait_for_a)
    waiter.start()

    # Completing another key calls notify_all, but must not release key A.
    other = read_model.get_langfuse_snapshot(env=env_b)
    assert other["state"] == "fresh"
    assert waiter_done.wait(timeout=0.1) is False

    release.set()
    leader.join(timeout=2)
    waiter.join(timeout=2)
    assert waiter_done.is_set()
    assert waiter_payload[0]["state"] == "fresh"


def test_cache_is_single_source_and_returns_stale_on_refresh_error(monkeypatch):
    calls = 0
    lock = threading.Lock()

    def build(*, days, env, resolved=None):
        del env, resolved
        nonlocal calls
        with lock:
            calls += 1
        return read_model._aggregate(
            [],
            days=days,
            generated_at="2026-07-29T01:00:00Z",
        )

    monkeypatch.setattr(read_model, "_build_remote_snapshot", build)
    first = read_model.get_langfuse_snapshot(env={"x": "y"})
    second = read_model.get_langfuse_snapshot(env={"x": "y"})

    assert first["state"] == "fresh"
    assert second["state"] == "fresh"
    assert calls == 1

    def fail(**_kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr(read_model, "_build_remote_snapshot", fail)
    stale = read_model.get_langfuse_snapshot(
        env={"x": "y"},
        force_refresh=True,
    )

    assert stale["available"] is True
    assert stale["state"] == "stale"
    assert stale["reason"] == "refresh_failed:RuntimeError"
