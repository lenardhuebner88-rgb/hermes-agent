from __future__ import annotations

import asyncio
import importlib
import os
import sqlite3
import subprocess
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace

import plugins.observability.board_facts as board_facts
from plugins.observability.board_facts import auxiliary_wrapper


def _response(
    *,
    model: str = "server-model",
    input_tokens: int = 101,
    output_tokens: int = 7,
) -> SimpleNamespace:
    return SimpleNamespace(
        id="response-id",
        model=model,
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="not retained"),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
        ),
    )


def _fake_aux_module(*, delay: float = 0.0) -> types.ModuleType:
    module = types.ModuleType("fake_auxiliary_client")

    def call_llm(
        task=None,
        *,
        provider=None,
        model=None,
        messages,
        stream=False,
        **kwargs,
    ):
        del task, provider, model, messages, kwargs
        if delay:
            time.sleep(delay)
        if stream:
            return iter(("first", "second", "third"))
        return _response()

    async def async_call_llm(
        task=None,
        *,
        provider=None,
        model=None,
        messages,
        **kwargs,
    ):
        del task, provider, model, messages, kwargs
        if delay:
            await asyncio.sleep(delay)
        return _response(model="async-server-model")

    module.call_llm = call_llm
    module.async_call_llm = async_call_llm
    return module


def _recording_callbacks():
    events = []

    def callback(phase):
        def record(**kwargs):
            events.append((phase, kwargs))

        return record

    return events, callback("pre"), callback("post"), callback("error")


def _install_fake(module, callbacks):
    pre, post, error = callbacks
    return auxiliary_wrapper.install_auxiliary_wrappers(
        pre_callback=pre,
        post_callback=post,
        error_callback=error,
        auxiliary_module=module,
    )


def _reload_board_facts(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_USAGE_FACTS_DB", str(tmp_path / "facts.db"))
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    return importlib.reload(board_facts)


def test_real_run_agent_import_rebinds_browser_and_vision(tmp_path):
    repo_root = Path(__file__).resolve().parents[4]
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - observability/board_facts\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["HERMES_HOME"] = str(hermes_home)
    env["HERMES_USAGE_FACTS_DB"] = str(tmp_path / "facts.db")
    env.pop("HERMES_SAFE_MODE", None)
    code = """
import run_agent
import agent.auxiliary_client as auxiliary_client
import tools.browser_tool as browser_tool
import tools.vision_tools as vision_tools

browser_proof = (
    browser_tool.call_llm is auxiliary_client.call_llm
    and getattr(browser_tool.call_llm, "_hermes_auxiliary_usage_wrapper", False)
)
vision_proof = (
    vision_tools.async_call_llm is auxiliary_client.async_call_llm
    and getattr(
        vision_tools.async_call_llm,
        "_hermes_auxiliary_usage_wrapper",
        False,
    )
)
print(f"tools.browser_tool.call_llm is wrapper: {browser_proof}")
print(f"tools.vision_tools.async_call_llm is wrapper: {vision_proof}")
assert browser_proof
assert vision_proof
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )
    print(completed.stdout, end="")
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == [
        "tools.browser_tool.call_llm is wrapper: True",
        "tools.vision_tools.async_call_llm is wrapper: True",
    ]


def test_sys_modules_sweep_rebinds_every_direct_sync_and_async_alias(monkeypatch):
    module = _fake_aux_module()
    original_sync = module.call_llm
    original_async = module.async_call_llm
    first_consumer = types.ModuleType("aux_consumer_one")
    second_consumer = types.ModuleType("aux_consumer_two")
    first_consumer.sync_alias = original_sync
    first_consumer.async_alias = original_async
    second_consumer.other_sync_alias = original_sync
    second_consumer.other_async_alias = original_async
    monkeypatch.setitem(sys.modules, first_consumer.__name__, first_consumer)
    monkeypatch.setitem(sys.modules, second_consumer.__name__, second_consumer)
    _, pre, post, error = _recording_callbacks()

    installation = _install_fake(module, (pre, post, error))

    assert first_consumer.sync_alias is installation.call_llm
    assert first_consumer.async_alias is installation.async_call_llm
    assert second_consumer.other_sync_alias is installation.call_llm
    assert second_consumer.other_async_alias is installation.async_call_llm
    assert installation.rebound_references >= 4


def test_keyword_splat_task_usage_duration_and_honest_provider_are_persisted(
    monkeypatch,
    tmp_path,
):
    plugin = _reload_board_facts(monkeypatch, tmp_path)
    module = _fake_aux_module(delay=0.005)
    installation = auxiliary_wrapper.install_auxiliary_wrappers(
        pre_callback=plugin.on_pre_llm_request,
        post_callback=plugin.on_post_llm_call,
        error_callback=plugin._on_auxiliary_error,
        auxiliary_module=module,
    )
    kwargs = {
        "task": "compression",
        "provider": "requested-provider",
        "model": "requested-model",
        "messages": [
            {
                "role": "user",
                "content": "full transcript must not be retained",
            }
        ],
    }

    response = installation.call_llm(**kwargs)

    assert response.model == "server-model"
    path = tmp_path / "facts.db"
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        fact = conn.execute("SELECT * FROM run_usage_facts").fetchone()
        call = conn.execute("SELECT * FROM run_llm_calls").fetchone()
        traces = conn.execute(
            "SELECT role, content FROM run_traces ORDER BY rowid"
        ).fetchall()

    assert fact["origin"] == "hermes_aux"
    assert fact["call_kind"] == "aux"
    assert fact["requested_provider"] == "requested-provider"
    assert fact["provider"] is None
    assert fact["requested_model"] == "requested-model"
    assert fact["model"] == "server-model"
    assert fact["wall_ms"] >= 1
    assert call["origin"] == "hermes_aux"
    assert call["provider"] is None
    assert call["model"] == "server-model"
    assert call["input_tokens"] == 101
    assert call["output_tokens"] == 7
    assert call["duration_ms"] >= 1
    assert [row["role"] for row in traces] == ["aux_metadata"]
    assert '"aux_task": "compression"' in traces[0]["content"]
    assert "full transcript" not in traces[0]["content"]


def test_none_task_uses_defined_unknown_fallback():
    module = _fake_aux_module()
    events, pre, post, error = _recording_callbacks()
    installation = _install_fake(module, (pre, post, error))

    installation.call_llm(task=None, messages=[])

    pre_event = next(event for phase, event in events if phase == "pre")
    assert pre_event["aux_task"] == "unknown"
    assert pre_event["request_messages"][0]["content"]["aux_task"] == "unknown"


def test_each_call_gets_unique_request_id_without_explicit_call_index():
    module = _fake_aux_module()
    events, pre, post, error = _recording_callbacks()
    installation = _install_fake(module, (pre, post, error))

    installation.call_llm(task="compression", messages=[])
    installation.call_llm(task="compression", messages=[])

    pre_events = [event for phase, event in events if phase == "pre"]
    request_ids = [event["api_request_id"] for event in pre_events]
    assert len(request_ids) == len(set(request_ids)) == 2
    assert all("call_index" not in event for event in pre_events)
    assert all("api_call_count" not in event for event in pre_events)


def test_async_wrapper_records_server_model_and_usage():
    module = _fake_aux_module()
    events, pre, post, error = _recording_callbacks()
    installation = _install_fake(module, (pre, post, error))

    response = asyncio.run(
        installation.async_call_llm(
            task="vision",
            provider="requested-provider",
            model="requested-model",
            messages=[],
        )
    )

    assert response.model == "async-server-model"
    post_event = next(event for phase, event in events if phase == "post")
    assert post_event["origin"] == "hermes_aux"
    assert post_event["call_kind"] == "aux"
    assert post_event["requested_provider"] == "requested-provider"
    assert "provider" not in post_event
    assert post_event["response_model"] == "async-server-model"
    assert post_event["usage"]["input_tokens"] == 101
    assert post_event["usage"]["output_tokens"] == 7
    assert "call_index" not in post_event
    assert "api_call_count" not in post_event


def test_stream_iterator_is_returned_by_identity_and_unconsumed():
    class TrackingIterator:
        def __init__(self):
            self._values = iter(("first", "second", "third"))
            self.next_calls = 0

        def __iter__(self):
            return self

        def __next__(self):
            self.next_calls += 1
            return next(self._values)

    stream_iterator = TrackingIterator()
    module = _fake_aux_module()

    def call_llm(
        task=None,
        *,
        provider=None,
        model=None,
        messages,
        stream=False,
        **kwargs,
    ):
        del task, provider, model, messages, kwargs
        assert stream is True
        return stream_iterator

    module.call_llm = call_llm
    events, pre, post, error = _recording_callbacks()
    installation = _install_fake(module, (pre, post, error))

    returned = installation.call_llm(
        task="moa_aggregator",
        messages=[],
        stream=True,
    )

    assert returned is stream_iterator
    assert stream_iterator.next_calls == 0
    assert list(returned) == ["first", "second", "third"]
    post_event = next(event for phase, event in events if phase == "post")
    assert post_event["call_kind"] == "stream_unmeasured"
    assert "usage" not in post_event
    assert "duration_ms" not in post_event


def test_instrumentation_failure_never_breaks_actual_call(monkeypatch):
    module = _fake_aux_module()
    sentinel = _response(model="actual-result")

    def call_llm(task=None, *, messages, **kwargs):
        del task, messages, kwargs
        return sentinel

    def broken_callback(**kwargs):
        del kwargs
        raise RuntimeError("intentional observer failure")

    module.call_llm = call_llm
    installation = _install_fake(
        module,
        (broken_callback, broken_callback, broken_callback),
    )

    def broken_wrapper_inner(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("intentional wrapper-internal failure")

    monkeypatch.setattr(
        auxiliary_wrapper,
        "_success_event_fields",
        broken_wrapper_inner,
    )
    assert installation.call_llm(task="compression", messages=[]) is sentinel


def test_double_installation_does_not_double_wrap_or_count():
    module = _fake_aux_module()
    original = module.call_llm
    events, pre, post, error = _recording_callbacks()

    first = _install_fake(module, (pre, post, error))
    second = _install_fake(module, (pre, post, error))
    result = second.call_llm(task="title_generation", messages=[])

    assert result.model == "server-model"
    assert first.call_llm is second.call_llm
    assert getattr(second.call_llm, "_hermes_auxiliary_original") is original
    assert [phase for phase, _event in events] == ["pre", "post"]


def test_aux_payload_exclusion_has_measured_volume_bound(monkeypatch, tmp_path):
    plugin = _reload_board_facts(monkeypatch, tmp_path)
    module = _fake_aux_module()
    installation = auxiliary_wrapper.install_auxiliary_wrappers(
        pre_callback=plugin.on_pre_llm_request,
        post_callback=plugin.on_post_llm_call,
        error_callback=plugin._on_auxiliary_error,
        auxiliary_module=module,
    )
    call_count = 32
    payload_bytes_per_call = 1024 * 1024
    transcript = "x" * payload_bytes_per_call

    for index in range(call_count):
        installation.call_llm(
            task=f"compression-{index}",
            provider="requested-provider",
            model="requested-model",
            messages=[{"role": "user", "content": transcript}],
        )

    path = tmp_path / "facts.db"
    with sqlite3.connect(path) as conn:
        trace_count, stored_trace_bytes, largest_trace_bytes = conn.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(LENGTH(content)), 0),
                   COALESCE(MAX(LENGTH(content)), 0)
            FROM run_traces
            """
        ).fetchone()
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
    allocated_db_bytes = page_size * page_count
    input_payload_bytes = call_count * payload_bytes_per_call
    db_bound_bytes = 256 * 1024

    print(
        "aux_trace_volume: "
        f"calls={call_count} "
        f"input_payload_bytes={input_payload_bytes} "
        f"stored_trace_bytes={stored_trace_bytes} "
        f"largest_trace_bytes={largest_trace_bytes} "
        f"db_allocated_bytes={allocated_db_bytes} "
        f"bound_bytes={db_bound_bytes}"
    )
    assert trace_count == call_count
    assert stored_trace_bytes <= call_count * 256
    assert largest_trace_bytes <= 256
    assert allocated_db_bytes <= db_bound_bytes
    with sqlite3.connect(path) as conn:
        retained = "\n".join(
            row[0] for row in conn.execute("SELECT content FROM run_traces")
        )
    assert "x" * 1000 not in retained


# ---------------------------------------------------------------------------
# Helper units
# ---------------------------------------------------------------------------

def test_observer_and_installation_dataclasses_are_frozen():
    """The instrumentation records must stay immutable — a wrapper that
    could rebind its own observer would silently stop measuring."""
    observer = auxiliary_wrapper._Observer(
        pre=lambda **kw: None, post=lambda **kw: None, error=lambda **kw: None
    )
    installation = auxiliary_wrapper.AuxiliaryWrapperInstallation(
        call_llm=lambda *a, **k: None,
        async_call_llm=lambda *a, **k: None,
        rebound_references=0,
    )
    try:
        observer.pre = lambda **kw: None
    except AttributeError:
        pass
    else:
        raise AssertionError("_Observer must be frozen")
    try:
        installation.rebound_references = 99
    except AttributeError:
        pass
    else:
        raise AssertionError("AuxiliaryWrapperInstallation must be frozen")


def test_aux_task_label_boundary_length_stays_verbatim():
    """A label of exactly the limit is kept verbatim — only LONGER labels
    get the hashed suffix; hashing the boundary loses the readable name."""
    text = "a" * auxiliary_wrapper._MAX_AUX_TASK_LABEL_CHARS
    assert auxiliary_wrapper._aux_task_label(text) == text
    longer = auxiliary_wrapper._aux_task_label(text + "b")
    assert "#" in longer
    assert len(longer) == auxiliary_wrapper._MAX_AUX_TASK_LABEL_CHARS


def test_has_usage_field_requires_a_present_non_none_value():
    """A field that exists but is None is NOT usable usage — counting it
    would pick the wrong provider normalisation on half-empty payloads."""
    assert auxiliary_wrapper._has_usage_field({"prompt_tokens": 5}, "prompt_tokens") is True
    assert auxiliary_wrapper._has_usage_field({"prompt_tokens": None}, "prompt_tokens") is False
    assert auxiliary_wrapper._has_usage_field({}, "prompt_tokens") is False
    obj = SimpleNamespace(prompt_tokens=None)
    assert auxiliary_wrapper._has_usage_field(obj, "prompt_tokens") is False
    assert auxiliary_wrapper._has_usage_field(SimpleNamespace(prompt_tokens=5), "prompt_tokens") is True


def test_normalized_usage_dispatches_on_which_token_fields_exist(monkeypatch):
    """Provider detection is field-driven: bare prompt/completion tokens
    take the default path, cache_* tokens the anthropic path — never the
    codex fallback when a real field is present."""
    seen = []

    def fake_normalize(usage, **kwargs):
        seen.append(kwargs)
        return SimpleNamespace(
            input_tokens=0, output_tokens=0, cache_read_tokens=0,
            cache_write_tokens=0, reasoning_tokens=0,
            cache_read_tokens_observed=False, cache_write_tokens_observed=False,
            reasoning_tokens_observed=False, total_tokens=0,
        )

    monkeypatch.setattr(auxiliary_wrapper, "normalize_usage", fake_normalize)

    auxiliary_wrapper._normalized_usage(SimpleNamespace(usage={"prompt_tokens": 5}))
    assert seen[-1] == {}

    auxiliary_wrapper._normalized_usage(
        SimpleNamespace(usage={"cache_creation_input_tokens": 3})
    )
    assert seen[-1] == {"provider": "anthropic"}

    auxiliary_wrapper._normalized_usage(SimpleNamespace(usage={"weird": 1}))
    assert seen[-1] == {"api_mode": "codex_responses"}
