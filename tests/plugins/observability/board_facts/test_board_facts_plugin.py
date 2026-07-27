from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path

import plugins.observability.board_facts as board_facts
import yaml


class _PluginContext:
    def __init__(self):
        self.hooks = {}

    def register_hook(self, event, callback):
        self.hooks[event] = callback


def _reload(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_USAGE_FACTS_DB", str(tmp_path / "facts.db"))
    return importlib.reload(board_facts)


def test_registers_five_handlers_on_six_existing_hooks(monkeypatch, tmp_path):
    plugin = _reload(monkeypatch, tmp_path)
    ctx = _PluginContext()

    plugin.register(ctx)

    assert set(ctx.hooks) == {
        "pre_api_request",
        "post_api_request",
        "pre_llm_call",
        "post_llm_call",
        "pre_tool_call",
        "post_tool_call",
    }
    assert ctx.hooks["pre_api_request"] is plugin.on_pre_llm_request
    assert ctx.hooks["post_api_request"] is plugin.on_post_llm_call
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
    plugin.on_pre_tool_call(
        **common,
        tool_name="terminal",
        tool_call_id="tool-42",
        args={"command": f"echo {secret}"},
    )
    plugin.on_post_tool_call(
        **common,
        tool_name="terminal",
        tool_call_id="tool-42",
        args={"command": f"echo {secret}"},
        result=f"failed with {secret}",
        status="error",
        error_type="tool_error",
        error_message=f"provider echoed {secret}",
        tool_output_tokens=5,
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
    assert fact["fallback_depth"] > 0
    assert fact["lane"] == "implementation"
    assert fact["billing_mode"] == "subscription_included"
    assert fact["serving_tier"] == "priority"
    assert fact["reasoning_effort"] == "high"
    assert fact["finish_reason"] == "tool_calls"
    assert fact["error_type"] == "tool_error"
    assert fact["temperature"] == 0.25
    assert fact["top_p"] == 0.8
    assert fact["source"] == "measured"
    assert call["response_id"] == "response-42"
    assert call["input_tokens"] == 101
    assert call["output_tokens"] == 22
    assert call["tool_call_count"] == 1
    assert call["tool_output_tokens"] == 5
    assert call["duration_ms"] == 1500
    assert {"system", "user", "assistant", "tool_args", "tool_result"} <= {
        row["role"] for row in traces
    }
    assert secret not in "\n".join(row["content"] for row in traces)


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
