from __future__ import annotations

import importlib
import sqlite3
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from agent.usage_pricing import normalize_usage
import plugins.observability.board_facts as board_facts
import pytest
import yaml


class _PluginContext:
    def __init__(self):
        self.hooks = {}

    def register_hook(self, event, callback):
        self.hooks[event] = callback


def _reload(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_USAGE_FACTS_DB", str(tmp_path / "facts.db"))
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    return importlib.reload(board_facts)


def test_registers_handlers_including_api_request_error(monkeypatch, tmp_path):
    plugin = _reload(monkeypatch, tmp_path)
    ctx = _PluginContext()

    plugin.register(ctx)

    assert set(ctx.hooks) == {
        "pre_api_request",
        "post_api_request",
        "api_request_error",
        "pre_llm_call",
        "post_llm_call",
        "pre_tool_call",
        "post_tool_call",
    }
    assert ctx.hooks["pre_api_request"] is plugin.on_pre_llm_request
    assert ctx.hooks["post_api_request"] is plugin.on_post_llm_call
    assert ctx.hooks["api_request_error"] is plugin.on_api_request_error
    assert ctx.hooks["post_llm_call"] is plugin.on_post_llm_call


def test_manifest_is_discovered_as_switchable_opt_in(monkeypatch, tmp_path):
    from hermes_cli import plugins as plugins_mod

    repo_root = Path(__file__).resolve().parents[4]
    manifest = yaml.safe_load(
        (
            repo_root
            / "plugins"
            / "observability"
            / "board_facts"
            / "plugin.yaml"
        ).read_text()
    )
    assert manifest["name"] == "board_facts"
    assert set(manifest["hooks"]) == {
        "pre_api_request",
        "post_api_request",
        "api_request_error",
        "pre_llm_call",
        "post_llm_call",
        "pre_tool_call",
        "post_tool_call",
    }

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    manager = plugins_mod.PluginManager()
    manager.discover_and_load()

    discovered = manager._plugins["observability/board_facts"]
    assert discovered.enabled is False
    assert "not enabled" in (discovered.error or "").lower()


def test_hooks_capture_routing_usage_and_redacted_traces(monkeypatch, tmp_path):
    plugin = _reload(monkeypatch, tmp_path)
    path = tmp_path / "facts.db"
    secret = "langfuse-secret-in-a-trace"
    monkeypatch.setenv("HERMES_LANGFUSE_SECRET_KEY", secret)
    common = {
        "task_run_id": "run-42",
        "task_id": "task-42",
        "turn_id": "turn-42",
        "api_request_id": "request-42",
        "session_id": "session-42",
        "api_call_count": 1,
        "provider": "xai-oauth",
        "model": "requested-grok",
        "requested_provider": "xai-oauth",
        "requested_model": "requested-grok",
        "lane": "implementation",
    }

    plugin.on_pre_llm_request(
        **common,
        request={
            "body": {
                "model": "requested-grok",
                "service_tier": "priority",
                "reasoning": {"effort": "high"},
                "temperature": 0.25,
                "top_p": 0.8,
            }
        },
        request_messages=[
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": f"prompt contains {secret}"},
        ],
    )
    plugin.on_post_llm_call(
        **common,
        response_model="grok-4.20",
        response={"id": "response-42", "model": "grok-4.20"},
        usage={
            "input_tokens": 101,
            "output_tokens": 22,
            "cache_read_tokens": 7,
            "cache_write_tokens": 3,
            "reasoning_tokens": 9,
            "total_tokens": 142,
            "prompt_tokens": 111,
        },
        finish_reason="tool_calls",
        first_token_ms=87,
        api_duration=1.5,
        context_window_limit=256000,
        assistant_tool_call_count=1,
        assistant_message={
            "role": "assistant",
            "content": f"assistant response {secret}",
        },
        request={
            "body": {
                "model": "requested-grok",
                "service_tier": "priority",
                "reasoning": {"effort": "high"},
                "temperature": 0.25,
                "top_p": 0.8,
            }
        },
    )
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        fact = conn.execute(
            "SELECT * FROM run_usage_facts WHERE run_id='run-42'"
        ).fetchone()
        call = conn.execute(
            "SELECT * FROM run_llm_calls WHERE run_id='run-42'"
        ).fetchone()
        traces = conn.execute(
            "SELECT role, content FROM run_traces WHERE run_id='run-42'"
        ).fetchall()

    assert fact["provider"] == "xai-oauth"
    assert fact["model"] == "grok-4.20"
    assert fact["requested_provider"] == "xai-oauth"
    assert fact["requested_model"] == "requested-grok"
    assert fact["fallback_depth"] is None
    assert fact["lane"] == "implementation"
    assert fact["billing_mode"] == "subscription_included"
    assert fact["serving_tier"] == "priority"
    assert fact["reasoning_effort"] == "high"
    assert fact["finish_reason"] == "tool_calls"
    assert fact["temperature"] == 0.25
    assert fact["top_p"] == 0.8
    assert fact["source"] == "measured"
    assert fact["first_token_ms"] == 87
    assert call["response_id"] == "response-42"
    assert call["input_tokens"] == 101
    assert call["output_tokens"] == 22
    assert call["duration_ms"] == 1500
    assert {"system", "user", "assistant"} <= {
        row["role"] for row in traces
    }
    assert secret not in "\n".join(row["content"] for row in traces)


def test_request_trace_persists_each_growing_message_once(
    monkeypatch,
    tmp_path,
):
    plugin = _reload(monkeypatch, tmp_path)
    path = tmp_path / "facts.db"
    messages = [
        {
            "role": "system" if index == 0 else "user",
            "content": f"trace-message-{index}",
        }
        for index in range(7)
    ]

    for call_index, message_count in enumerate((3, 5, 7), start=1):
        plugin.on_pre_llm_request(
            task_run_id="run-growing-trace",
            turn_id="turn-growing-trace",
            api_request_id=f"request-{call_index}",
            session_id="session-growing-trace",
            api_call_count=call_index,
            provider="test",
            model="test-model",
            request_messages=messages[:message_count],
        )

    with sqlite3.connect(path) as conn:
        trace_count = conn.execute(
            "SELECT COUNT(*) FROM run_traces "
            "WHERE run_id='run-growing-trace'"
        ).fetchone()[0]

    assert trace_count == 7


def test_request_trace_records_changed_content_at_an_old_position(
    monkeypatch,
    tmp_path,
):
    plugin = _reload(monkeypatch, tmp_path)
    path = tmp_path / "facts.db"
    initial = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "original"},
        {"role": "assistant", "content": "answer"},
    ]
    changed = [dict(message) for message in initial]
    changed[1]["content"] = "compressed replacement"

    for call_index, messages in enumerate((initial, changed), start=1):
        plugin.on_pre_llm_request(
            task_run_id="run-changed-trace",
            turn_id="turn-changed-trace",
            api_request_id=f"request-{call_index}",
            session_id="session-changed-trace",
            api_call_count=call_index,
            provider="test",
            model="test-model",
            request_messages=messages,
        )

    with sqlite3.connect(path) as conn:
        contents = [
            row[0]
            for row in conn.execute(
                "SELECT content FROM run_traces "
                "WHERE run_id='run-changed-trace'"
            )
        ]

    assert len(contents) == 4
    assert any("original" in content for content in contents)
    assert any("compressed replacement" in content for content in contents)


def test_request_trace_restart_does_not_replay_persisted_messages(
    monkeypatch,
    tmp_path,
):
    plugin = _reload(monkeypatch, tmp_path)
    path = tmp_path / "facts.db"
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    common = {
        "task_run_id": "run-restarted-trace",
        "turn_id": "turn-restarted-trace",
        "session_id": "session-restarted-trace",
        "provider": "test",
        "model": "test-model",
        "request_messages": messages,
    }

    plugin.on_pre_llm_request(
        **common,
        api_request_id="request-before-restart",
        api_call_count=1,
    )
    plugin = importlib.reload(board_facts)
    plugin.on_pre_llm_request(
        **common,
        api_request_id="request-after-restart",
        api_call_count=2,
    )

    with sqlite3.connect(path) as conn:
        trace_count = conn.execute(
            "SELECT COUNT(*) FROM run_traces "
            "WHERE run_id='run-restarted-trace'"
        ).fetchone()[0]

    assert trace_count == 3


@pytest.mark.parametrize(
    ("model", "expected"),
    (
        ("gpt-5.6-terra", 1_050_000),
        ("gpt-5.6-sol", 1_050_000),
        ("gpt-5.6-luna", 1_050_000),
        ("claude-opus-4-8", 1_000_000),
        ("claude-fable-5", 1_000_000),
        ("grok-4.5", 500_000),
        ("kimi-k2.7-code", None),
        ("qwen3.8-max-preview", None),
        ("k3", None),
    ),
)
def test_context_window_limit_uses_only_exact_static_model_keys(
    monkeypatch,
    tmp_path,
    model,
    expected,
):
    plugin = _reload(monkeypatch, tmp_path)
    path = tmp_path / "facts.db"

    plugin.on_pre_llm_request(
        task_run_id=f"run-{model}",
        turn_id=f"turn-{model}",
        api_request_id=f"request-{model}",
        session_id=f"session-{model}",
        api_call_count=1,
        provider="test",
        model=model,
        request={"body": {"model": model}},
    )

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        fact = conn.execute(
            "SELECT context_window_limit, context_window_limit_source "
            "FROM run_usage_facts WHERE run_id=?",
            (f"run-{model}",),
        ).fetchone()

    assert fact["context_window_limit"] == expected
    assert fact["context_window_limit_source"] == (
        "derived" if expected is not None else None
    )


def test_post_tool_call_uses_exact_live_kwargs_and_derives_output_chars(
    monkeypatch,
    tmp_path,
):
    plugin = _reload(monkeypatch, tmp_path)
    path = tmp_path / "facts.db"

    plugin.on_post_tool_call(
        tool_name="terminal",
        args={"command": "printf four"},
        result="four",
        task_id="task-tool",
        session_id="session-tool",
        tool_call_id="tool-call-1",
        turn_id="turn-tool",
        api_request_id="request-tool",
        duration_ms=12,
        status="ok",
        error_type=None,
        error_message=None,
        middleware_trace=[],
    )

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        fact = conn.execute(
            "SELECT * FROM run_usage_facts WHERE run_id='turn-tool'"
        ).fetchone()
        call = conn.execute(
            "SELECT * FROM run_llm_calls WHERE run_id='turn-tool'"
        ).fetchone()

    assert fact["tool_call_count"] == 1
    assert fact["tool_output_chars"] == 4
    assert call["tool_call_count"] == 1
    assert call["tool_output_chars"] == 4


def test_versioned_response_model_does_not_invent_fallback_depth(
    monkeypatch,
    tmp_path,
):
    plugin = _reload(monkeypatch, tmp_path)
    path = tmp_path / "facts.db"
    request = {"body": {"model": "claude-opus-4-8"}}

    plugin.on_pre_llm_request(
        turn_id="turn-versioned",
        api_request_id="request-versioned",
        session_id="session-versioned",
        api_call_count=1,
        provider="anthropic",
        model="claude-opus-4-8",
        request=request,
    )
    plugin.on_post_llm_call(
        turn_id="turn-versioned",
        api_request_id="request-versioned",
        session_id="session-versioned",
        api_call_count=1,
        provider="anthropic",
        model="claude-opus-4-8",
        response={
            "model": "claude-opus-4-8-20260115",
            "usage": {"input_tokens": 10, "output_tokens": 2},
        },
        request=request,
    )

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        fact = conn.execute(
            "SELECT * FROM run_usage_facts WHERE run_id='turn-versioned'"
        ).fetchone()

    assert fact["requested_model"] == "claude-opus-4-8"
    assert fact["model"] == "claude-opus-4-8-20260115"
    assert fact["fallback_depth"] is None


def test_api_request_error_preserves_previous_successful_call(
    monkeypatch,
    tmp_path,
):
    plugin = _reload(monkeypatch, tmp_path)
    path = tmp_path / "facts.db"

    plugin.on_post_llm_call(
        task_id="task-error",
        turn_id="turn-error",
        api_request_id="request-success",
        session_id="session-error",
        provider="anthropic",
        model="claude-opus-4-8",
        api_call_count=1,
        api_duration=0.5,
        response={
            "model": "claude-opus-4-8-20260115",
            "usage": {
                "input_tokens": 12,
                "output_tokens": 7,
                "cache_read_tokens": 0,
                "cache_read_tokens_observed": True,
                "cache_write_tokens": 0,
                "cache_write_tokens_observed": True,
                "reasoning_tokens": 0,
                "reasoning_tokens_observed": True,
            },
        },
        request={"body": {"model": "claude-opus-4-8"}},
    )
    plugin.on_api_request_error(
        task_id="task-error",
        turn_id="turn-error",
        api_request_id="request-failed",
        session_id="session-error",
        platform="cli",
        model="claude-opus-4-8",
        provider="anthropic",
        base_url="https://api.anthropic.com",
        api_mode="anthropic_messages",
        api_call_count=2,
        api_duration=1.25,
        started_at=100.0,
        ended_at=101.25,
        status_code=429,
        retry_count=1,
        max_retries=3,
        retryable=True,
        reason="rate_limit",
        error={
            "type": "RateLimitError",
            "message": "request rate limited",
        },
        request={"method": "POST", "body": {"model": "claude-opus-4-8"}},
    )

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        fact = conn.execute(
            "SELECT * FROM run_usage_facts WHERE run_id='turn-error'"
        ).fetchone()
        calls = conn.execute(
            "SELECT * FROM run_llm_calls WHERE run_id='turn-error'"
        ).fetchall()

    assert fact["error_type"] == "RateLimitError"
    assert fact["provider"] == "anthropic"
    assert fact["requested_model"] == "claude-opus-4-8"
    assert fact["input_tokens"] == 12
    assert fact["output_tokens"] == 7
    assert fact["llm_call_count"] == 1
    assert len(calls) == 1
    assert calls[0]["input_tokens"] == 12
    assert calls[0]["error_type"] is None


@pytest.mark.parametrize(
    ("body", "expected"),
    (
        ({"reasoning": {"effort": "high"}}, "high"),
        ({"extra_body": {"reasoning": {"effort": "low"}}}, "low"),
        (
            {"thinking": {"type": "enabled", "budget_tokens": 16_000}},
            "high",
        ),
        (
            {
                "thinking": {"type": "adaptive"},
                "output_config": {"effort": "max"},
            },
            "max",
        ),
    ),
)
def test_sampling_fields_reads_real_reasoning_transport_shapes(
    monkeypatch,
    tmp_path,
    body,
    expected,
):
    plugin = _reload(monkeypatch, tmp_path)

    fields = plugin._sampling_fields({"request": {"body": body}})

    assert fields["reasoning_effort"] == expected


def test_optional_usage_buckets_distinguish_missing_from_explicit_zero(
    monkeypatch,
    tmp_path,
):
    plugin = _reload(monkeypatch, tmp_path)
    path = tmp_path / "facts.db"
    missing = asdict(
        normalize_usage(
            SimpleNamespace(prompt_tokens=10, completion_tokens=2),
            provider="openai",
            api_mode="chat_completions",
        )
    )
    explicit_zero = asdict(
        normalize_usage(
            SimpleNamespace(
                prompt_tokens=10,
                completion_tokens=2,
                prompt_tokens_details=SimpleNamespace(
                    cached_tokens=0,
                    cache_write_tokens=0,
                ),
                completion_tokens_details=SimpleNamespace(
                    reasoning_tokens=0,
                ),
            ),
            provider="openai",
            api_mode="chat_completions",
        )
    )

    for run_id, usage in (
        ("turn-missing", missing),
        ("turn-explicit-zero", explicit_zero),
    ):
        plugin.on_post_llm_call(
            turn_id=run_id,
            api_request_id=f"request-{run_id}",
            session_id=f"session-{run_id}",
            api_call_count=1,
            provider="openai",
            model="gpt-test",
            response={"model": "gpt-test", "usage": usage},
            request={"body": {"model": "gpt-test"}},
        )

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        missing_fact = conn.execute(
            "SELECT * FROM run_usage_facts WHERE run_id='turn-missing'"
        ).fetchone()
        zero_fact = conn.execute(
            "SELECT * FROM run_usage_facts "
            "WHERE run_id='turn-explicit-zero'"
        ).fetchone()

    for field in (
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
    ):
        assert missing_fact[field] is None
        assert zero_fact[field] == 0


def test_trace_purge_runs_at_most_once_per_plugin_process(
    monkeypatch,
    tmp_path,
):
    plugin = _reload(monkeypatch, tmp_path)
    purge_calls = []
    monkeypatch.setattr(
        plugin,
        "purge_expired_traces",
        lambda: purge_calls.append(True),
    )

    for suffix in ("one", "two"):
        plugin.on_pre_llm_request(
            turn_id=f"turn-{suffix}",
            api_request_id=f"request-{suffix}",
            session_id=f"session-{suffix}",
            api_call_count=1,
            provider="anthropic",
            model="claude-opus-4-8",
            request={"body": {"model": "claude-opus-4-8"}},
        )

    assert purge_calls == [True]


def test_hooks_are_fail_soft_when_database_path_is_unusable(monkeypatch, tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("file")
    monkeypatch.setenv("HERMES_USAGE_FACTS_DB", str(blocker / "facts.db"))
    plugin = importlib.reload(board_facts)

    assert (
        plugin.on_pre_llm_request(
            task_run_id="run-fail-soft",
            api_call_count=1,
            provider="anthropic",
            model="claude-opus-4-8",
            messages=[{"role": "user", "content": "hello"}],
        )
        is None
    )


def test_session_correlation_starts_a_new_run_per_turn(monkeypatch, tmp_path):
    plugin = _reload(monkeypatch, tmp_path)
    path = tmp_path / "facts.db"

    for turn in ("turn-one", "turn-two"):
        plugin.on_pre_llm_request(
            session_id="shared-session",
            turn_id=turn,
            api_request_id=f"request-{turn}",
            api_call_count=1,
            provider="anthropic",
            model="requested-model",
            request_messages=[{"role": "user", "content": turn}],
        )
        plugin.on_post_llm_call(
            session_id="shared-session",
            turn_id=turn,
            api_request_id=f"request-{turn}",
            api_call_count=1,
            provider="anthropic",
            model="requested-model",
            response_model=f"actual-{turn}",
            usage={"input_tokens": 1, "output_tokens": 1},
        )

    # Turn-finalization event: must not overwrite the provider-observed model.
    plugin.on_post_llm_call(
        session_id="shared-session",
        turn_id="turn-one",
        model="requested-model",
    )

    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT run_id, model FROM run_usage_facts ORDER BY run_id"
        ).fetchall()

    assert rows == [
        ("turn-one", "actual-turn-one"),
        ("turn-two", "actual-turn-two"),
    ]
