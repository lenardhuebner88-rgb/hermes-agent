from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path

import pytest

from scripts.langfuse_worker_audit import (
    LangfuseGenerationProbeIncomplete,
    _RejectDashboardRedirectHandler,
    _RejectLangfuseRedirectHandler,
    _authenticated_dashboard_request,
    _configured_langfuse_generation_probe,
    _langfuse_public_request,
    build_control_surface_live_smoke,
    build_live_smoke_contract,
    main as audit_main,
)


def _seed_databases(
    tmp_path: Path,
    *,
    include_first_token: bool = True,
    worker_runtime: str = "hermes",
) -> tuple[Path, Path]:
    usage_path = tmp_path / "usage.db"
    with sqlite3.connect(usage_path) as conn:
        conn.execute(
            "CREATE TABLE run_usage_facts (run_id TEXT PRIMARY KEY, task_run_id TEXT, "
            "task_id TEXT, board TEXT, captured_at TEXT)"
        )
        conn.execute(
            "INSERT INTO run_usage_facts VALUES ('usage-1', '42', 'task-1', 'default', "
            "'2026-07-29T20:00:00+00:00')"
        )

    kanban_path = tmp_path / "kanban.db"
    with sqlite3.connect(kanban_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE task_runs (
                id INTEGER PRIMARY KEY, task_id TEXT, outcome TEXT,
                worker_exit_code INTEGER, started_at INTEGER, ended_at INTEGER,
                metadata TEXT
            );
            CREATE TABLE worker_run_timeline_events (
                task_run_id INTEGER, event_kind TEXT, observed_at_ms INTEGER,
                source TEXT, task_id TEXT, board TEXT, chain_root_id TEXT, profile TEXT
            );
            CREATE TABLE worker_run_terminal_facts (
                task_run_id INTEGER, worker_exit_kind TEXT, worker_exit_code INTEGER,
                worker_protocol_state TEXT, task_outcome TEXT, end_reason TEXT,
                task_id TEXT, board TEXT
            );
            INSERT INTO worker_run_terminal_facts VALUES
                (42, 'exited', 0, 'completed', 'completed', 'kanban_complete', 'task-1', 'default');
            """
        )
        conn.execute(
            "INSERT INTO task_runs VALUES (42, 'task-1', 'completed', 0, 1, 2, ?)",
            (json.dumps({"worker_runtime": worker_runtime}),),
        )
        kinds = [
            "queued",
            "claimed",
            "spawn_started",
            "process_started",
            "first_llm_request",
            "ended",
        ]
        if include_first_token:
            kinds.insert(-1, "first_token")
        conn.executemany(
            "INSERT INTO worker_run_timeline_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (42, kind, index, "test", "task-1", "default", "task-1", "coder")
                for index, kind in enumerate(kinds, start=1)
            ],
        )
    return usage_path, kanban_path


def test_live_smoke_contract_passes_only_with_exact_trace_usage_and_lifecycle(tmp_path: Path) -> None:
    usage_path, kanban_path = _seed_databases(tmp_path)

    report = build_live_smoke_contract(
        task_run_id=42,
        usage_path=usage_path,
        kanban_path=kanban_path,
        trace_probe=lambda: [
            {"id": "trace-1234567890", "metadata": {
                "task_run_id": "42", "task_id": "task-1", "board": "default"
            }}
        ],
        generation_probe=lambda _trace_id: [
            {
                "id": "generation-1234567890",
                "traceId": "trace-1234567890",
                "type": "GENERATION",
                "metadata": {
                    "task_run_id": "42", "task_id": "task-1", "board": "default"
                },
            }
        ],
    )

    assert report["status"] == "pass"
    assert report["contract_version"] == 2
    assert report["schema"] == {
        "worker_runtime_facts_version": 1,
        "usage_correlation_version": 1,
    }
    assert report["worker_runtime"] == "hermes"
    assert report["langfuse"] == {
        "credentials_configured": True,
        "tcp_http_reachable": True,
        "authenticated_public_api_readable": True,
        "exact_trace_link": True,
        "exact_task_board_link": True,
        "trace_id_short": "trace-12…7890",
        "exact_generation_link": True,
        "generation_id_short": "generati…7890",
        "generation_trace_match": True,
    }
    assert report["usage_fact"]["exact_rows"] == 1
    assert report["usage_fact"]["exact_task_board_match"] is True
    assert report["lifecycle"]["missing_events"] == []
    assert report["terminal"]["worker_exit_code"] == 0


def test_live_smoke_contract_prefers_explicit_kanban_task_identity(
    tmp_path: Path,
) -> None:
    usage_path, kanban_path = _seed_databases(tmp_path)
    metadata = {
        "task_run_id": "42",
        "task_id": "session-42",
        "kanban_task_id": "task-1",
        "board": "default",
    }

    report = build_live_smoke_contract(
        task_run_id=42,
        usage_path=usage_path,
        kanban_path=kanban_path,
        trace_probe=lambda: [{"id": "trace-1", "metadata": metadata}],
        generation_probe=lambda _trace_id: [{
            "id": "generation-1",
            "traceId": "trace-1",
            "metadata": metadata,
        }],
    )

    assert report["status"] == "pass"
    assert report["langfuse"]["exact_task_board_link"] is True


def test_live_smoke_contract_fails_without_exact_generation(tmp_path: Path) -> None:
    usage_path, kanban_path = _seed_databases(tmp_path)

    report = build_live_smoke_contract(
        task_run_id=42,
        usage_path=usage_path,
        kanban_path=kanban_path,
        trace_probe=lambda: [
            {"id": "trace-1", "metadata": {
                "task_run_id": "42", "task_id": "task-1", "board": "default"
            }}
        ],
        generation_probe=lambda _trace_id: [],
    )

    assert report["status"] == "fail"
    assert report["langfuse"]["exact_trace_link"] is True
    assert report["langfuse"]["exact_generation_link"] is False
    assert report["langfuse"]["generation_trace_match"] is False


def test_live_smoke_contract_fails_when_generation_belongs_to_another_trace(
    tmp_path: Path,
) -> None:
    usage_path, kanban_path = _seed_databases(tmp_path)

    report = build_live_smoke_contract(
        task_run_id=42,
        usage_path=usage_path,
        kanban_path=kanban_path,
        trace_probe=lambda: [
            {"id": "trace-1", "metadata": {
                "task_run_id": "42", "task_id": "task-1", "board": "default"
            }}
        ],
        generation_probe=lambda _trace_id: [
            {
                "id": "generation-1",
                "traceId": "trace-other",
                "metadata": {
                    "task_run_id": "42", "task_id": "task-1", "board": "default"
                },
            }
        ],
    )

    assert report["status"] == "fail"
    assert report["langfuse"]["exact_generation_link"] is False
    assert report["langfuse"]["generation_trace_match"] is False


def test_live_smoke_contract_selects_generation_from_matching_trace(
    tmp_path: Path,
) -> None:
    usage_path, kanban_path = _seed_databases(tmp_path)
    requested_trace_ids: list[str] = []

    def generations(trace_id: str) -> list[dict]:
        requested_trace_ids.append(trace_id)
        if trace_id == "trace-2":
            return [{
                "id": "generation-2",
                "traceId": "trace-2",
                "metadata": {
                    "task_run_id": "42", "task_id": "task-1", "board": "default"
                },
            }]
        return [{
            "id": "foreign-generation",
            "traceId": "foreign-trace",
            "metadata": {
                "task_run_id": "42", "task_id": "task-1", "board": "default"
            },
        }]

    report = build_live_smoke_contract(
        task_run_id=42,
        usage_path=usage_path,
        kanban_path=kanban_path,
        trace_probe=lambda: [
            {"id": "trace-1", "metadata": {
                "task_run_id": "42", "task_id": "task-1", "board": "default"
            }},
            {"id": "trace-2", "metadata": {
                "task_run_id": "42", "task_id": "task-1", "board": "default"
            }},
        ],
        generation_probe=generations,
    )

    assert requested_trace_ids == ["trace-1", "trace-2"]
    assert report["status"] == "pass"
    assert report["langfuse"]["trace_id_short"] == "trace-2"
    assert report["langfuse"]["generation_id_short"] == "generation-2"


@pytest.mark.parametrize(
    ("dimension", "wrong_value"),
    [("task_id", "task-other"), ("board", "other")],
)
def test_live_smoke_contract_rejects_cross_identity_langfuse_records(
    tmp_path: Path,
    dimension: str,
    wrong_value: str,
) -> None:
    usage_path, kanban_path = _seed_databases(tmp_path)
    metadata = {
        "task_run_id": "42",
        "task_id": "task-1",
        "board": "default",
    }
    metadata[dimension] = wrong_value

    report = build_live_smoke_contract(
        task_run_id=42,
        usage_path=usage_path,
        kanban_path=kanban_path,
        trace_probe=lambda: [{"id": "trace-wrong", "metadata": metadata}],
        generation_probe=lambda _trace_id: [{
            "id": "generation-wrong",
            "traceId": "trace-wrong",
            "metadata": metadata,
        }],
    )

    assert report["status"] == "fail"
    assert report["langfuse"]["exact_trace_link"] is False


def test_live_smoke_contract_rejects_unobserved_worker_protocol(
    tmp_path: Path,
) -> None:
    usage_path, kanban_path = _seed_databases(tmp_path)
    with sqlite3.connect(kanban_path) as conn:
        conn.execute(
            "UPDATE worker_run_terminal_facts "
            "SET worker_protocol_state='unobserved' WHERE task_run_id=42"
        )
    metadata = {
        "task_run_id": "42",
        "task_id": "task-1",
        "board": "default",
    }

    report = build_live_smoke_contract(
        task_run_id=42,
        usage_path=usage_path,
        kanban_path=kanban_path,
        trace_probe=lambda: [{"id": "trace-1", "metadata": metadata}],
        generation_probe=lambda _trace_id: [{
            "id": "generation-1",
            "traceId": "trace-1",
            "metadata": metadata,
        }],
    )

    assert report["status"] == "fail"
    assert report["terminal"]["worker_protocol_state"] == "unobserved"


def test_live_smoke_contract_rejects_usage_fact_from_wrong_board(
    tmp_path: Path,
) -> None:
    usage_path, kanban_path = _seed_databases(tmp_path)
    with sqlite3.connect(usage_path) as conn:
        conn.execute(
            "UPDATE run_usage_facts SET board='other' WHERE task_run_id='42'"
        )
    metadata = {
        "task_run_id": "42",
        "task_id": "task-1",
        "board": "default",
    }

    report = build_live_smoke_contract(
        task_run_id=42,
        usage_path=usage_path,
        kanban_path=kanban_path,
        trace_probe=lambda: [{"id": "trace-1", "metadata": metadata}],
        generation_probe=lambda _trace_id: [{
            "id": "generation-1",
            "traceId": "trace-1",
            "metadata": metadata,
        }],
    )

    assert report["status"] == "fail"
    assert report["usage_fact"]["exact_rows"] == 0
    assert report["usage_fact"]["exact_task_board_match"] is False


def test_live_smoke_contract_is_structurally_red_when_langfuse_is_unreachable(tmp_path: Path) -> None:
    usage_path, kanban_path = _seed_databases(tmp_path)

    def unreachable() -> list[dict[str, object]]:
        raise RuntimeError("secret-key-value must never be copied")

    report = build_live_smoke_contract(
        task_run_id=42,
        usage_path=usage_path,
        kanban_path=kanban_path,
        trace_probe=unreachable,
    )

    assert report["status"] == "fail"
    assert report["langfuse"] == {
        "credentials_configured": None,
        "tcp_http_reachable": False,
        "authenticated_public_api_readable": False,
        "exact_trace_link": False,
        "trace_id_short": None,
        "exact_generation_link": False,
        "generation_id_short": None,
        "generation_trace_match": False,
        "error_type": "RuntimeError",
    }
    assert "secret-key-value" not in json.dumps(report)


def test_live_smoke_contract_distinguishes_http_auth_failure(tmp_path: Path) -> None:
    usage_path, kanban_path = _seed_databases(tmp_path)

    def unauthorized() -> list[dict[str, object]]:
        cause = urllib.error.HTTPError("https://langfuse.invalid", 401, "secret", Message(), None)
        raise RuntimeError("secret-key-value must never be copied") from cause

    report = build_live_smoke_contract(
        task_run_id=42,
        usage_path=usage_path,
        kanban_path=kanban_path,
        trace_probe=unauthorized,
    )

    assert report["status"] == "fail"
    assert report["langfuse"] == {
        "credentials_configured": True,
        "tcp_http_reachable": True,
        "authenticated_public_api_readable": False,
        "exact_trace_link": False,
        "trace_id_short": None,
        "exact_generation_link": False,
        "generation_id_short": None,
        "generation_trace_match": False,
        "error_type": "HTTPError",
        "http_status": 401,
    }
    assert "secret" not in json.dumps(report)


def _dashboard_payload(*, usage_state: str = "fresh", fact_rows: int | None = 12) -> dict:
    summary = {} if fact_rows is None else {"fact_rows": fact_rows}
    return {
        "langfuse": {"state": "fresh"},
        "usage": {
            "state": usage_state,
            "summary": summary,
            "cache": {"age_seconds": 1},
        },
    }


def _langfuse_env() -> dict[str, str]:
    return {
        "HERMES_LANGFUSE_BASE_URL": "http://127.0.0.1:3000",
        "HERMES_LANGFUSE_PUBLIC_KEY": "pk-test",
        "HERMES_LANGFUSE_SECRET_KEY": "sk-test",
    }


def test_configured_generation_probe_is_trace_scoped_bounded_and_content_free(
    monkeypatch,
) -> None:
    for name, value in _langfuse_env().items():
        monkeypatch.setenv(name, value)
    requested_urls: list[str] = []

    def request_json(
        url: str,
        authorization: str,
        *,
        timeout: float,
        **_kwargs,
    ) -> dict:
        requested_urls.append(url)
        assert authorization.startswith("Basic ")
        assert 0 < timeout <= 4.0
        if len(requested_urls) == 1:
            return {
                "data": [
                    {
                        "id": "generation-1",
                        "traceId": "trace-1",
                        "metadata": {
                            "task_run_id": "42",
                            "raw_prompt": "must-not-be-retained",
                        },
                        "secret_note": "must-not-be-retained",
                        "input": "must-not-be-retained",
                        "output": "must-not-be-retained",
                    },
                    {"id": None, "traceId": "trace-1"},
                    {"id": "foreign", "traceId": "trace-other"},
                ],
                "meta": {"totalPages": "2"},
            }
        return {"data": [], "meta": {"totalPages": "2"}}

    rows = _configured_langfuse_generation_probe(
        "trace-1",
        request_json=request_json,
    )

    assert len(requested_urls) == 2
    assert all("traceId=trace-1" in url for url in requested_urls)
    assert rows == [
        {
            "id": "generation-1",
            "traceId": "trace-1",
            "metadata": {"task_run_id": "42"},
        }
    ]
    assert "must-not-be-retained" not in json.dumps(rows)


def test_langfuse_redirect_handler_never_forwards_authorization() -> None:
    handler = _RejectLangfuseRedirectHandler()
    request = urllib.request.Request("http://127.0.0.1:3000/api/public/traces")
    request.add_header("Authorization", "Basic secret")

    redirected = handler.redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "https://attacker.example/collect",
    )

    assert redirected is None


def test_langfuse_public_request_rejects_oversized_response(
    monkeypatch,
) -> None:
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, limit: int) -> bytes:
            return b"x" * limit

    class Opener:
        def open(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(
        "scripts.langfuse_worker_audit.build_opener",
        lambda *_handlers: Opener(),
    )

    with pytest.raises(
        LangfuseGenerationProbeIncomplete,
        match="response_too_large",
    ):
        _langfuse_public_request(
            "http://127.0.0.1:3000/api/public/traces",
            "Basic secret",
            timeout=1.0,
            expected_origin=("http", "127.0.0.1", 3000),
            max_bytes=16,
        )


def test_live_smoke_contract_bounds_duplicate_exact_traces(
    tmp_path: Path,
) -> None:
    usage_path, kanban_path = _seed_databases(tmp_path)
    metadata = {
        "task_run_id": "42",
        "task_id": "task-1",
        "board": "default",
    }
    generation_calls: list[str] = []

    report = build_live_smoke_contract(
        task_run_id=42,
        usage_path=usage_path,
        kanban_path=kanban_path,
        trace_probe=lambda: [
            {"id": f"trace-{index}", "metadata": metadata}
            for index in range(6)
        ],
        generation_probe=lambda trace_id: generation_calls.append(trace_id) or [],
    )

    assert report["status"] == "fail"
    assert report["langfuse"]["reason"] == "candidate_trace_limit"
    assert generation_calls == []


def test_configured_generation_probe_fails_closed_at_page_cap(monkeypatch) -> None:
    for name, value in _langfuse_env().items():
        monkeypatch.setenv(name, value)

    def full_page(*_args, **_kwargs) -> dict:
        return {
            "data": [
                {"id": f"generation-{index}", "traceId": "trace-1"}
                for index in range(100)
            ],
            "meta": {"totalPages": 999},
        }

    with pytest.raises(LangfuseGenerationProbeIncomplete, match="page_limit"):
        _configured_langfuse_generation_probe(
            "trace-1",
            request_json=full_page,
            max_pages=2,
            max_rows=1_000,
        )


def test_live_smoke_contract_classifies_generation_payload_invalid_as_reachable(
    tmp_path: Path,
) -> None:
    usage_path, kanban_path = _seed_databases(tmp_path)

    def payload_invalid(_trace_id: str) -> list[dict]:
        from scripts.langfuse_worker_audit import LangfusePayloadInvalid

        raise LangfusePayloadInvalid("secret response must not be copied")

    report = build_live_smoke_contract(
        task_run_id=42,
        usage_path=usage_path,
        kanban_path=kanban_path,
        trace_probe=lambda: [
            {"id": "trace-1", "metadata": {
                "task_run_id": "42", "task_id": "task-1", "board": "default"
            }}
        ],
        generation_probe=payload_invalid,
    )

    assert report["status"] == "fail"
    assert report["langfuse"]["credentials_configured"] is True
    assert report["langfuse"]["tcp_http_reachable"] is True
    assert report["langfuse"]["authenticated_public_api_readable"] is False
    assert report["langfuse"]["reason"] == "payload_invalid"
    assert "secret response" not in json.dumps(report)


@pytest.mark.parametrize(
    ("failure", "reason", "status"),
    [
        (
            urllib.error.HTTPError(
                "http://127.0.0.1", 401, "unauthorized", Message(), None
            ),
            "http_error",
            401,
        ),
        (json.JSONDecodeError("invalid", "not-json", 0), "payload_invalid", None),
    ],
)
def test_control_surface_smoke_classifies_langfuse_failures_without_details(
    failure: Exception,
    reason: str,
    status: int | None,
) -> None:
    def fail(*_args, **_kwargs) -> dict:
        raise failure

    report = build_control_surface_live_smoke(
        dashboard_base_url="http://127.0.0.1:9119",
        env=_langfuse_env(),
        langfuse_request=fail,
        dashboard_request=lambda _url: _dashboard_payload(),
    )

    assert report["langfuse"]["reason"] == reason
    assert report["langfuse"].get("http_status") == status
    assert "error_type" not in report["langfuse"]
    assert "unauthorized" not in json.dumps(report)


@pytest.mark.parametrize(
    ("cause", "reason", "status"),
    [
        (
            urllib.error.HTTPError(
                "http://127.0.0.1", 401, "secret", Message(), None
            ),
            "http_error",
            401,
        ),
        (
            urllib.error.URLError("secret-network-reason"),
            "unreachable",
            None,
        ),
        (
            json.JSONDecodeError("secret response", "secret body", 0),
            "payload_invalid",
            None,
        ),
    ],
)
def test_control_surface_smoke_classifies_wrapped_production_client_failures(
    cause: Exception,
    reason: str,
    status: int | None,
) -> None:
    def fail(*_args, **_kwargs) -> dict:
        raise RuntimeError("production client summary") from cause

    report = build_control_surface_live_smoke(
        dashboard_base_url="http://127.0.0.1:9119",
        env=_langfuse_env(),
        langfuse_request=fail,
        dashboard_request=lambda _url: _dashboard_payload(),
    )

    assert report["langfuse"]["reason"] == reason
    assert report["langfuse"].get("http_status") == status
    assert report["langfuse"]["configured_host_checked"] is True
    assert report["dashboard"]["sample_count"] == 10
    assert report["dashboard"]["fact_rows"] == 12
    assert report["status"] == "fail"
    encoded = json.dumps(report)
    assert "secret-network-reason" not in encoded
    assert "secret response" not in encoded
    assert "secret body" not in encoded
    assert "production client summary" not in encoded


def test_control_surface_smoke_classifies_production_non_object_payload(
    monkeypatch,
) -> None:
    from plugins.kanban.dashboard import langfuse_read_model

    class NonObjectResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self) -> bytes:
            return b"[]"

    monkeypatch.setattr(
        langfuse_read_model,
        "open_credentialed_url",
        lambda *_args, **_kwargs: NonObjectResponse(),
    )

    report = build_control_surface_live_smoke(
        dashboard_base_url="http://127.0.0.1:9119",
        env=_langfuse_env(),
        dashboard_request=lambda _url: _dashboard_payload(),
    )

    assert report["langfuse"] == {
        "state": "absent",
        "reason": "payload_invalid",
        "configured_host_checked": True,
    }
    assert report["dashboard"]["sample_count"] == 10
    assert report["status"] == "fail"


def test_control_surface_cli_returns_structured_exit_three_for_default_client_failure(
    monkeypatch,
    capsys,
) -> None:
    from plugins.kanban.dashboard import langfuse_read_model

    def fail(*_args, **_kwargs) -> dict:
        raise RuntimeError("Langfuse unreachable") from urllib.error.URLError(
            "secret-network-reason"
        )

    monkeypatch.setattr(langfuse_read_model, "_request_json", fail)
    monkeypatch.setattr(
        "scripts.langfuse_worker_audit._authenticated_dashboard_request",
        lambda *_args, **_kwargs: lambda _url: _dashboard_payload(),
    )
    for name, value in _langfuse_env().items():
        monkeypatch.setenv(name, value)

    exit_code = audit_main(
        [
            "--control-surface-smoke-url",
            "http://127.0.0.1:9119",
            "--no-prompt",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 3
    assert report["status"] == "fail"
    assert report["langfuse"] == {
        "state": "absent",
        "reason": "unreachable",
        "configured_host_checked": True,
    }
    assert report["dashboard"]["sample_count"] == 10
    assert report["dashboard"]["fact_rows"] == 12
    assert "secret-network-reason" not in json.dumps(report)


def test_dashboard_auth_accepts_canonical_login_response(monkeypatch) -> None:
    from scripts import smoke_health_status_auth as auth_smoke

    monkeypatch.setattr(
        "scripts.langfuse_worker_audit.build_opener",
        lambda *_handlers: object(),
    )
    monkeypatch.setattr(auth_smoke, "_read_password", lambda *_args, **_kwargs: "secret")
    monkeypatch.setattr(
        auth_smoke,
        "_json_request",
        lambda *_args, **_kwargs: {"ok": True, "next": "/"},
    )
    monkeypatch.setattr(auth_smoke, "_text_request", lambda *_args, **_kwargs: "")

    probe = _authenticated_dashboard_request(
        "http://127.0.0.1:9119",
        provider="basic",
        username="operator",
        password_env="HERMES_DASHBOARD_PASSWORD",
        no_prompt=True,
    )

    assert callable(probe)


def test_dashboard_auth_rejects_login_response_without_ok(monkeypatch) -> None:
    from scripts import smoke_health_status_auth as auth_smoke

    monkeypatch.setattr(
        "scripts.langfuse_worker_audit.build_opener",
        lambda *_handlers: object(),
    )
    monkeypatch.setattr(auth_smoke, "_read_password", lambda *_args, **_kwargs: "secret")
    monkeypatch.setattr(
        auth_smoke,
        "_json_request",
        lambda *_args, **_kwargs: {"authenticated": True},
    )

    with pytest.raises(ValueError, match="dashboard_authentication_failed"):
        _authenticated_dashboard_request(
            "http://127.0.0.1:9119",
            provider="basic",
            username="operator",
            password_env="HERMES_DASHBOARD_PASSWORD",
            no_prompt=True,
        )


def test_dashboard_auth_rejects_non_loopback_before_any_request(monkeypatch) -> None:
    opener_called = False

    def forbidden_opener(*_args, **_kwargs):
        nonlocal opener_called
        opener_called = True
        raise AssertionError("network setup must not run")

    monkeypatch.setattr(
        "scripts.langfuse_worker_audit.build_opener",
        forbidden_opener,
    )

    with pytest.raises(ValueError, match="dashboard_url_invalid"):
        _authenticated_dashboard_request(
            "https://external.example",
            provider="basic",
            username="operator",
            password_env="HERMES_DASHBOARD_PASSWORD",
            no_prompt=True,
        )

    assert opener_called is False


def test_dashboard_auth_rejects_redirects_before_cookie_or_token_forwarding(
    monkeypatch,
) -> None:
    from scripts import smoke_health_status_auth as auth_smoke

    captured_handlers: list[object] = []
    dummy_opener = object()

    def capture_opener(*handlers):
        captured_handlers.extend(handlers)
        return dummy_opener

    monkeypatch.setattr(
        "scripts.langfuse_worker_audit.build_opener",
        capture_opener,
    )
    monkeypatch.setattr(auth_smoke, "_read_password", lambda *_args, **_kwargs: "secret")
    monkeypatch.setattr(
        auth_smoke,
        "_json_request",
        lambda *_args, **_kwargs: {"ok": True, "next": "/"},
    )
    monkeypatch.setattr(auth_smoke, "_text_request", lambda *_args, **_kwargs: "")

    _authenticated_dashboard_request(
        "http://127.0.0.1:9119",
        provider="basic",
        username="operator",
        password_env="HERMES_DASHBOARD_PASSWORD",
        no_prompt=True,
    )

    redirect_handler = next(
        handler
        for handler in captured_handlers
        if isinstance(handler, _RejectDashboardRedirectHandler)
    )
    request = urllib.request.Request(
        "http://127.0.0.1:9119/api/plugins/kanban/stats/observability",
        headers={"X-Hermes-Session-Token": "secret-token"},
    )
    assert (
        redirect_handler.redirect_request(
            request,
            None,
            302,
            "Found",
            Message(),
            "http://127.0.0.1:9120/capture",
        )
        is None
    )


def test_control_surface_smoke_requires_fresh_usage_with_known_fact_rows() -> None:
    def langfuse(*_args, **_kwargs) -> dict:
        return {"data": [{"id": "safe"}], "meta": {"totalPages": 1}}

    report = build_control_surface_live_smoke(
        dashboard_base_url="http://127.0.0.1:9119",
        env=_langfuse_env(),
        langfuse_request=langfuse,
        dashboard_request=lambda _url: _dashboard_payload(
            usage_state="partial", fact_rows=None
        ),
    )

    assert report["langfuse"]["state"] == "fresh"
    assert report["dashboard"]["budget"]["passed"] is True
    assert report["dashboard"]["usage_acceptable"] is False
    assert report["dashboard"]["sample_count"] >= 10
    assert report["status"] == "fail"


def test_control_surface_smoke_fails_when_one_warm_read_exceeds_wall_budget() -> None:
    def langfuse(*_args, **_kwargs) -> dict:
        return {"data": [{"id": "safe"}], "meta": {"totalPages": 1}}

    dashboard_calls = 0

    def dashboard(_url: str) -> dict:
        nonlocal dashboard_calls
        dashboard_calls += 1
        if dashboard_calls == 2:
            time.sleep(0.31)
        return _dashboard_payload()

    report = build_control_surface_live_smoke(
        dashboard_base_url="http://127.0.0.1:9119",
        warm_calls=5,
        env=_langfuse_env(),
        langfuse_request=langfuse,
        dashboard_request=dashboard,
    )

    assert report["dashboard"]["wall_ms"]["maximum"] >= 300
    assert report["dashboard"]["wall_ms"]["mean"] < 300
    assert report["dashboard"]["budget"]["passed"] is False
    assert report["status"] == "fail"


def test_control_surface_smoke_scan_row_limit_fails_closed() -> None:
    def langfuse(*_args, **_kwargs) -> dict:
        return {
            "data": [{"id": "one"}, {"id": "two"}],
            "meta": {"totalPages": 2},
        }

    report = build_control_surface_live_smoke(
        dashboard_base_url="http://127.0.0.1:9119",
        env=_langfuse_env(),
        langfuse_request=langfuse,
        dashboard_request=lambda _url: _dashboard_payload(),
        scan_max_rows=1,
    )

    assert report["langfuse"]["state"] == "partial"
    assert report["langfuse"]["full_scan"]["reason"] == "row_limit"
    assert report["status"] == "fail"


def test_control_surface_smoke_does_not_mask_programming_errors() -> None:
    def broken(*_args, **_kwargs) -> dict:
        raise AssertionError("programming bug")

    with pytest.raises(AssertionError, match="programming bug"):
        build_control_surface_live_smoke(
            dashboard_base_url="http://127.0.0.1:9119",
            env=_langfuse_env(),
            langfuse_request=broken,
            dashboard_request=lambda _url: _dashboard_payload(),
        )


def test_control_surface_smoke_does_not_mask_unexpected_runtime_errors() -> None:
    def broken(*_args, **_kwargs) -> dict:
        raise RuntimeError("programming bug")

    with pytest.raises(RuntimeError, match="programming bug"):
        build_control_surface_live_smoke(
            dashboard_base_url="http://127.0.0.1:9119",
            env=_langfuse_env(),
            langfuse_request=broken,
            dashboard_request=lambda _url: _dashboard_payload(),
        )


def test_control_surface_smoke_runbook_uses_canonical_cookie_login() -> None:
    readme = (
        Path(__file__).resolve().parents[2]
        / "plugins"
        / "observability"
        / "langfuse"
        / "README.md"
    ).read_text(encoding="utf-8")

    assert "http://127.0.0.1:9119" in readme
    assert "scripts/smoke_health_status_auth.py" in readme
    assert "in-memory cookie" in readme
    assert "HERMES_DASHBOARD_PASSWORD" in readme
    assert "--warm-calls 10" in readme
    assert "HERMES_DASHBOARD_TOKEN" not in readme
    assert "127.0.0.1:8642" not in readme


def test_live_smoke_contract_distinguishes_url_failure(tmp_path: Path) -> None:
    usage_path, kanban_path = _seed_databases(tmp_path)

    def unreachable() -> list[dict[str, object]]:
        raise RuntimeError("secret-key-value must never be copied") from urllib.error.URLError(
            "secret-network-reason"
        )

    report = build_live_smoke_contract(
        task_run_id=42,
        usage_path=usage_path,
        kanban_path=kanban_path,
        trace_probe=unreachable,
    )

    assert report["status"] == "fail"
    assert report["langfuse"]["credentials_configured"] is True
    assert report["langfuse"]["tcp_http_reachable"] is False
    assert report["langfuse"]["authenticated_public_api_readable"] is False
    assert report["langfuse"]["error_type"] == "URLError"
    assert "secret-network-reason" not in json.dumps(report)


def test_live_smoke_contract_classifies_invalid_json_as_reachable(tmp_path: Path) -> None:
    usage_path, kanban_path = _seed_databases(tmp_path)

    def invalid_json() -> list[dict[str, object]]:
        raise RuntimeError("response contents must never be copied") from json.JSONDecodeError(
            "secret response", "secret body", 0
        )

    report = build_live_smoke_contract(
        task_run_id=42,
        usage_path=usage_path,
        kanban_path=kanban_path,
        trace_probe=invalid_json,
    )

    assert report["status"] == "fail"
    assert report["langfuse"]["credentials_configured"] is True
    assert report["langfuse"]["tcp_http_reachable"] is True
    assert report["langfuse"]["authenticated_public_api_readable"] is False
    assert report["langfuse"]["error_type"] == "JSONDecodeError"
    assert "secret body" not in json.dumps(report)


def test_live_smoke_contract_reports_missing_credentials(tmp_path: Path, monkeypatch) -> None:
    usage_path, kanban_path = _seed_databases(tmp_path)
    for name in (
        "HERMES_LANGFUSE_BASE_URL",
        "HERMES_LANGFUSE_HOST",
        "HERMES_LANGFUSE_PUBLIC_KEY",
        "HERMES_LANGFUSE_SECRET_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    report = build_live_smoke_contract(
        task_run_id=42,
        usage_path=usage_path,
        kanban_path=kanban_path,
    )

    assert report["status"] == "fail"
    assert report["langfuse"]["credentials_configured"] is False
    assert report["langfuse"]["tcp_http_reachable"] is False
    assert report["langfuse"]["authenticated_public_api_readable"] is False
    assert report["langfuse"]["error_type"] == "LangfuseCredentialsMissing"


def test_live_smoke_contract_requires_first_token_after_model_request(tmp_path: Path) -> None:
    usage_path, kanban_path = _seed_databases(tmp_path, include_first_token=False)

    report = build_live_smoke_contract(
        task_run_id=42,
        usage_path=usage_path,
        kanban_path=kanban_path,
        trace_probe=lambda: [
            {"id": "trace-1", "metadata": {
                "task_run_id": "42", "task_id": "task-1", "board": "default"
            }}
        ],
        generation_probe=lambda _trace_id: [],
    )

    assert report["status"] == "fail"
    assert report["lifecycle"]["missing_events"] == ["first_token"]
    assert report["lifecycle"]["first_token_required"] is True


def test_live_smoke_contract_does_not_require_first_token_before_model_request(
    tmp_path: Path,
) -> None:
    usage_path, kanban_path = _seed_databases(tmp_path, include_first_token=False)
    with sqlite3.connect(kanban_path) as conn:
        conn.execute(
            "DELETE FROM worker_run_timeline_events WHERE event_kind = 'first_llm_request'"
        )

    report = build_live_smoke_contract(
        task_run_id=42,
        usage_path=usage_path,
        kanban_path=kanban_path,
        trace_probe=lambda: [],
        generation_probe=lambda _trace_id: [],
    )

    assert report["status"] == "fail"
    assert report["lifecycle"]["missing_events"] == ["first_llm_request"]
    assert report["lifecycle"]["first_token_required"] is False


def test_live_smoke_contract_marks_claude_cli_run_ineligible_not_lifecycle_defective(
    tmp_path: Path,
) -> None:
    usage_path, kanban_path = _seed_databases(tmp_path, worker_runtime="claude-cli")
    with sqlite3.connect(kanban_path) as conn:
        conn.execute("DELETE FROM worker_run_timeline_events WHERE event_kind = 'first_llm_request'")
        conn.execute("DELETE FROM worker_run_timeline_events WHERE event_kind = 'first_token'")

    report = build_live_smoke_contract(
        task_run_id=42,
        usage_path=usage_path,
        kanban_path=kanban_path,
        trace_probe=lambda: [],
        generation_probe=lambda _trace_id: [],
    )

    assert report["status"] == "fail"
    assert report["worker_runtime"] == "claude-cli"
    assert report["runtime_eligible"] is False
    assert report["lifecycle"]["assessment"] == "not_applicable"
    assert report["lifecycle"]["missing_events"] == []


def test_control_surface_smoke_proves_full_partial_and_warm_budget() -> None:
    observations = [
        {
            "id": f"generation-{index}",
            "type": "GENERATION",
            "metadata": {"kanban_run_id": str(index)},
            "startTime": "2026-07-29T20:00:00Z",
            "endTime": "2026-07-29T20:00:01Z",
            "usageDetails": {"total": 10},
        }
        for index in range(3)
    ]

    def langfuse_request(url: str, _authorization: str, *, timeout: float) -> dict:
        assert timeout > 0
        query = url.split("?", 1)[1]
        params = dict(part.split("=", 1) for part in query.split("&"))
        page = int(params["page"])
        limit = int(params["limit"])
        start = (page - 1) * limit
        return {
            "data": observations[start : start + limit],
            "meta": {"totalPages": (len(observations) + limit - 1) // limit},
        }

    dashboard_calls: list[str] = []

    def dashboard_request(url: str) -> dict:
        dashboard_calls.append(url)
        busy_until = time.process_time() + 0.001
        while time.process_time() < busy_until:
            pass
        return {
            "langfuse": {"state": "fresh"},
            "usage": {
                "state": "fresh",
                "summary": {"fact_rows": 94_682},
                "cache": {"age_seconds": len(dashboard_calls)},
            },
        }

    report = build_control_surface_live_smoke(
        dashboard_base_url="http://127.0.0.1:9119",
        days=7,
        warm_calls=5,
        page_size=2,
        env={
            "HERMES_LANGFUSE_BASE_URL": "http://127.0.0.1:3000",
            "HERMES_LANGFUSE_PUBLIC_KEY": "pk-secret",
            "HERMES_LANGFUSE_SECRET_KEY": "sk-secret",
        },
        langfuse_request=langfuse_request,
        dashboard_request=dashboard_request,
    )

    assert report["status"] == "pass", report
    assert report["langfuse"]["public_api_page"] == {
        "authenticated": True,
        "rows": 2,
    }
    assert report["langfuse"]["full_scan"]["state"] == "fresh"
    assert report["langfuse"]["full_scan"]["coverage"]["scan_truncated"] is False
    assert report["langfuse"]["limited_scan"]["state"] == "partial"
    assert report["langfuse"]["limited_scan"]["reason"] == "intentional_limit"
    assert report["langfuse"]["limited_scan"]["summary"]["count"]["lower_bound"] is True
    assert report["langfuse"]["limited_scan"]["coverage"]["window_coverage"] is None
    assert report["dashboard"]["authenticated"] is True
    assert report["dashboard"]["warm_calls"] == 5
    assert report["dashboard"]["fact_rows"] == 94_682
    assert report["dashboard"]["cache_age_seconds"] == [2, 3, 4, 5, 6]
    assert report["dashboard"]["client_cpu_ms"]["maximum"] >= 0.5
    assert report["dashboard"]["budget"]["server_cpu_budget"] == "not_observable_over_http"
    assert report["dashboard"]["budget"]["wall_maximum_limit_ms"] == 300
    assert "cpu_mean_limit_ms" not in report["dashboard"]["budget"]
    assert report["dashboard"]["budget"]["passed"] is True
    assert len(dashboard_calls) == 6
    encoded = json.dumps(report)
    assert "dashboard-secret" not in encoded
    assert "pk-secret" not in encoded
    assert "sk-secret" not in encoded


def test_control_surface_smoke_keeps_usage_available_when_langfuse_is_unreachable() -> None:
    def fail_langfuse(*_args, **_kwargs) -> dict:
        raise urllib.error.URLError("connection refused at http://secret-host")

    report = build_control_surface_live_smoke(
        dashboard_base_url="http://127.0.0.1:9119",
        env={
            "HERMES_LANGFUSE_BASE_URL": "http://127.0.0.1:3000",
            "HERMES_LANGFUSE_PUBLIC_KEY": "pk-secret",
            "HERMES_LANGFUSE_SECRET_KEY": "sk-secret",
        },
        langfuse_request=fail_langfuse,
        dashboard_request=lambda _url: {
            "langfuse": {"state": "absent", "reason": "read_failed:RuntimeError"},
            "usage": {
                "state": "fresh",
                "summary": {"fact_rows": 12},
                "cache": {"age_seconds": 1},
            },
        },
    )

    assert report["status"] == "fail"
    assert report["langfuse"] == {
        "state": "absent",
        "reason": "unreachable",
        "configured_host_checked": True,
    }
    assert report["dashboard"]["usage_state"] == "fresh"
    assert report["dashboard"]["langfuse_state"] == "absent"
    assert "secret-host" not in json.dumps(report)
