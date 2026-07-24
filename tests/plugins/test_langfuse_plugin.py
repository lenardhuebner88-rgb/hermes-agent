"""Tests for the bundled observability/langfuse plugin."""
from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path

import pytest

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO_ROOT / "plugins" / "observability" / "langfuse"


@pytest.fixture(autouse=True)
def _restore_sys_modules_after_purge(restore_sys_modules):
    """Keep langfuse / hermes_cli.config purges from leaking into later files.

    Several tests here force a clean plugin re-read via
    ``sys.modules.pop("plugins.observability.langfuse")`` (and in a few
    places ``hermes_cli.config``) then re-import. Without a restore, later
    files in the same worker keep import-time bindings to the orphaned
    pre-purge objects while lazy imports resolve to the reimport
    (module-identity split) — same class as the security-guidance /
    empty-tool-name leaks (see ``tests/_module_isolation.py``).
    ``restore_sys_modules`` snapshots before the test body and puts the
    table (plus parent-package attributes) back on teardown.
    """
    yield


# ---------------------------------------------------------------------------
# Manifest + layout
# ---------------------------------------------------------------------------

class TestManifest:
    def test_plugin_directory_exists(self):
        assert PLUGIN_DIR.is_dir()
        assert (PLUGIN_DIR / "plugin.yaml").exists()
        assert (PLUGIN_DIR / "__init__.py").exists()

    def test_manifest_fields(self):
        data = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())
        assert data["name"] == "langfuse"
        assert data["version"]
        # All six hooks the plugin implements.
        assert set(data["hooks"]) == {
            "pre_api_request", "post_api_request",
            "pre_llm_call", "post_llm_call",
            "pre_tool_call", "post_tool_call",
        }
        # Required env vars are the user-facing HERMES_ prefixed keys.
        assert "HERMES_LANGFUSE_PUBLIC_KEY" in data["requires_env"]
        assert "HERMES_LANGFUSE_SECRET_KEY" in data["requires_env"]


# ---------------------------------------------------------------------------
# Plugin discovery: langfuse is opt-in (not loaded unless explicitly enabled).
# This guards against someone accidentally re-introducing a per-hook
# load_config() gate or making the plugin auto-load.
# ---------------------------------------------------------------------------

class TestDiscovery:
    def test_plugin_is_discovered_as_standalone_opt_in(self, tmp_path, monkeypatch):
        """Scanner should find the plugin but NOT load it by default."""
        from hermes_cli import plugins as plugins_mod

        # Isolated HERMES_HOME so we don't read the developer's config.yaml.
        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        manager = plugins_mod.PluginManager()
        manager.discover_and_load()

        # observability/langfuse appears in the plugin registry …
        loaded = manager._plugins.get("observability/langfuse")
        assert loaded is not None, "plugin not discovered"
        # … but is not loaded (opt-in default → no config.yaml means nothing enabled)
        assert loaded.enabled is False
        assert "not enabled" in (loaded.error or "").lower()


# ---------------------------------------------------------------------------
# Runtime gate: _get_langfuse() returns None and caches _INIT_FAILED when
# credentials are missing. Guards against regressing toward the rejected
# per-hook load_config() design.
# ---------------------------------------------------------------------------

class TestRuntimeGate:
    def _fresh_plugin(self):
        """Import the plugin module fresh (clears any cached client)."""
        mod_name = "plugins.observability.langfuse"
        sys.modules.pop(mod_name, None)
        return importlib.import_module(mod_name)

    def test_get_langfuse_returns_none_without_credentials(self, monkeypatch):
        for k in (
            "HERMES_LANGFUSE_PUBLIC_KEY", "HERMES_LANGFUSE_SECRET_KEY",
            "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY",
        ):
            monkeypatch.delenv(k, raising=False)

        langfuse_plugin = self._fresh_plugin()
        assert langfuse_plugin._get_langfuse() is None

    def test_default_disabled_has_zero_client_initializations_and_sends(self, monkeypatch):
        for key in ("HERMES_LANGFUSE_PUBLIC_KEY", "HERMES_LANGFUSE_SECRET_KEY",
                    "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
            monkeypatch.delenv(key, raising=False)
        mod = self._fresh_plugin()
        counters = {"client_initializations": 0, "send_calls": 0}

        class GuardClient:
            def __init__(self, **kwargs):
                counters["client_initializations"] += 1

            def start_as_current_observation(self, **kwargs):
                counters["send_calls"] += 1
                raise AssertionError("disabled plugin attempted egress")

        monkeypatch.setattr(mod, "Langfuse", GuardClient)
        mod.on_pre_llm_request(task_id="t", session_id="s", request_messages=[{"role": "user", "content": "x"}])
        assert counters == {"client_initializations": 0, "send_calls": 0}

    def test_explicitly_disabled_gate_blocks_initialized_client_and_egress(self, monkeypatch):
        """Credentials alone must never opt an installation into telemetry."""
        monkeypatch.setenv("HERMES_LANGFUSE_PUBLIC_KEY", "pk-lf-synthetic")
        monkeypatch.setenv("HERMES_LANGFUSE_SECRET_KEY", "sk-lf-synthetic")
        monkeypatch.setenv("HERMES_LANGFUSE_ENABLED", "false")
        mod = self._fresh_plugin()
        counters = {"client_initializations": 0, "send_calls": 0}

        class GuardClient:
            def __init__(self, **kwargs):
                counters["client_initializations"] += 1

            def start_as_current_observation(self, **kwargs):
                counters["send_calls"] += 1

        monkeypatch.setattr(mod, "Langfuse", GuardClient)
        assert mod._get_langfuse() is None
        mod.on_pre_llm_request(task_id="t", session_id="s", request_messages=[])
        assert counters == {"client_initializations": 0, "send_calls": 0}

    def test_constructor_failure_timeout_and_invalid_config_leave_hook_result_unchanged(self, monkeypatch):
        """Plugin initialization errors must have the same hook result as no plugin."""
        monkeypatch.setenv("HERMES_LANGFUSE_PUBLIC_KEY", "pk-lf-synthetic")
        monkeypatch.setenv("HERMES_LANGFUSE_SECRET_KEY", "sk-lf-synthetic")
        monkeypatch.setenv("HERMES_LANGFUSE_ENABLED", "true")
        monkeypatch.setenv("HERMES_LANGFUSE_TIMEOUT_SECONDS", "not-a-number")
        mod = self._fresh_plugin()
        attempts = []

        class UnreachableClient:
            def __init__(self, **kwargs):
                attempts.append(kwargs)
                raise TimeoutError("synthetic connection timeout")

        monkeypatch.setattr(mod, "Langfuse", UnreachableClient)
        plugin_free_result = None
        plugin_failure_result = mod.on_pre_llm_request(
            task_id="synthetic-task", session_id="synthetic-session", request_messages=[]
        )
        assert plugin_failure_result is plugin_free_result
        assert attempts[0]["timeout"] == 5.0

    def test_nan_timeout_falls_back_and_leaves_hook_result_unchanged(self, monkeypatch):
        """Non-finite SDK timeouts must not escape the plugin boundary."""
        monkeypatch.setenv("HERMES_LANGFUSE_PUBLIC_KEY", "pk-lf-synthetic")
        monkeypatch.setenv("HERMES_LANGFUSE_SECRET_KEY", "sk-lf-synthetic")
        monkeypatch.setenv("HERMES_LANGFUSE_ENABLED", "true")
        monkeypatch.setenv("HERMES_LANGFUSE_TIMEOUT_SECONDS", "NaN")
        mod = self._fresh_plugin()
        attempts = []

        class UnreachableClient:
            def __init__(self, **kwargs):
                attempts.append(kwargs)
                raise TimeoutError("synthetic connection timeout")

        monkeypatch.setattr(mod, "Langfuse", UnreachableClient)
        assert mod.on_pre_llm_request(
            task_id="synthetic-task", session_id="synthetic-session", request_messages=[]
        ) is None
        assert attempts[0]["timeout"] == 5.0

    def test_get_langfuse_caches_failure_no_config_load(self, monkeypatch):
        """A miss must be cached — no per-hook config.yaml reads, no env re-reads."""
        for k in (
            "HERMES_LANGFUSE_PUBLIC_KEY", "HERMES_LANGFUSE_SECRET_KEY",
            "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY",
        ):
            monkeypatch.delenv(k, raising=False)

        langfuse_plugin = self._fresh_plugin()

        # Prime the cache with one call.
        assert langfuse_plugin._get_langfuse() is None

        # Now block os.environ.get — a correctly-cached plugin must not
        # touch env again.
        import os
        called = {"n": 0}
        real_get = os.environ.get

        def tracking_get(key, default=None):
            if key.startswith(("HERMES_LANGFUSE_", "LANGFUSE_")):
                called["n"] += 1
            return real_get(key, default)

        monkeypatch.setattr(os.environ, "get", tracking_get)

        for _ in range(20):
            assert langfuse_plugin._get_langfuse() is None

        assert called["n"] == 0, (
            f"_get_langfuse() re-read env {called['n']} times after cache miss — "
            "it should short-circuit via _INIT_FAILED"
        )

    def test_get_langfuse_does_not_import_hermes_config(self, monkeypatch):
        """The plugin must not re-read config.yaml per hook."""
        for k in (
            "HERMES_LANGFUSE_PUBLIC_KEY", "HERMES_LANGFUSE_SECRET_KEY",
            "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY",
        ):
            monkeypatch.delenv(k, raising=False)

        # Drop any cached import of hermes_cli.config.
        sys.modules.pop("hermes_cli.config", None)

        langfuse_plugin = self._fresh_plugin()
        for _ in range(20):
            langfuse_plugin._get_langfuse()

        assert "hermes_cli.config" not in sys.modules, (
            "langfuse plugin imported hermes_cli.config — regression toward "
            "the rejected per-hook load_config() design"
        )


# ---------------------------------------------------------------------------
# Hooks are inert when the client is unavailable.
# ---------------------------------------------------------------------------

class TestHooksInert:
    def test_hooks_noop_without_client(self, monkeypatch):
        """All 6 hooks must return without raising when _get_langfuse() is None."""
        for k in (
            "HERMES_LANGFUSE_PUBLIC_KEY", "HERMES_LANGFUSE_SECRET_KEY",
            "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY",
        ):
            monkeypatch.delenv(k, raising=False)

        sys.modules.pop("plugins.observability.langfuse", None)
        import importlib
        mod = importlib.import_module("plugins.observability.langfuse")

        # Each hook should just return; no exceptions.
        mod.on_pre_llm_call(task_id="t", session_id="s", messages=[{"role": "user", "content": "hi"}])
        mod.on_pre_llm_request(task_id="t", session_id="s", api_call_count=1, request_messages=[])
        mod.on_post_llm_call(task_id="t", session_id="s", api_call_count=1)
        mod.on_pre_tool_call(tool_name="read_file", args={}, task_id="t", session_id="s")
        mod.on_post_tool_call(tool_name="read_file", args={}, result="ok", task_id="t", session_id="s")


class TestPluginFailureInvariance:
    @pytest.mark.parametrize("export_error", [ConnectionError, TimeoutError, ValueError])
    def test_connection_timeout_and_invalid_export_leave_result_and_outcome_unchanged(
        self, monkeypatch, export_error
    ):
        """A broken sidecar must be observational only for a Hermes run."""
        mod = importlib.import_module("plugins.observability.langfuse")
        mod._TRACE_STATE.clear()

        class UnavailableClient:
            def create_trace_id(self, **_kwargs):
                raise export_error("synthetic export failure")

        def run_with(client):
            monkeypatch.setattr(mod, "_get_langfuse", lambda: client)
            hook_result = mod.on_pre_llm_request(
                task_id="task", session_id="session", api_call_count=1,
                request_messages=[{"role": "user", "content": "synthetic request"}],
            )
            # These are the application-owned values the sidecar must not alter.
            return {"answer": "synthetic result"}, {"status": "completed"}, hook_result

        control = run_with(None)
        failed_export = run_with(UnavailableClient())
        assert failed_export == control


class TestPayloadSanitization:
    def test_correlation_allowlist_keeps_only_hook_values(self):
        mod = importlib.import_module("plugins.observability.langfuse")
        metadata = mod._correlation_metadata(
            task_run_id="run-1", task_id="task-1", chain_id="chain-1", lane="coder",
            provider="openai", model="test-model",
            billing_snapshot={"plan": "pro", "subscription": "included", "secret": "never-export"},
        )
        assert metadata == {
            "task_run_id": "run-1", "task_id": "task-1", "chain_id": "chain-1",
            "lane": "coder", "provider": "openai", "model": "test-model",
            "billing_snapshot": {"plan": "pro", "subscription": "included"},
        }

    def test_safe_value_redacts_base64_data_uri_instead_of_truncating(self):
        sys.modules.pop("plugins.observability.langfuse", None)
        import importlib
        mod = importlib.import_module("plugins.observability.langfuse")

        payload = "data:image/png;base64," + ("a" * 20000)
        result = mod._safe_value(payload)

        assert result == {
            "type": "data_uri",
            "media_type": "image/png",
            "omitted": True,
            "length": len(payload),
        }

    def test_serialize_messages_redacts_data_uri_parts(self):
        sys.modules.pop("plugins.observability.langfuse", None)
        import importlib
        mod = importlib.import_module("plugins.observability.langfuse")

        payload = "data:image/jpeg;base64," + ("b" * 20000)
        serialized = mod._serialize_messages([
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": payload}}]}
        ])

        assert serialized[0]["content"] == mod._redact_raw_content([
            {"type": "image_url", "image_url": {"url": payload}}
        ])
        assert payload not in repr(serialized)


class TestKanbanWorkerTraceMetadata:
    @staticmethod
    def _start_root_trace(mod):
        recorded = {}

        class _RootSpan:
            def set_trace_io(self, **_kwargs):
                pass

        class _RootContext:
            def __enter__(self):
                return _RootSpan()

            def __exit__(self, *_args):
                return False

        class _Client:
            def create_trace_id(self, **_kwargs):
                return "trace-id"

            def start_as_current_observation(self, **kwargs):
                recorded.update(kwargs)
                return _RootContext()

        mod._start_root_trace(
            "task-key", task_id="turn-task", session_id="session", platform="cli",
            provider="provider", model="model", api_mode="chat", messages=[],
            client=_Client(),
        )
        return recorded

    def test_root_trace_stamps_kanban_worker_identity_and_tag(self, monkeypatch):
        mod = importlib.import_module("plugins.observability.langfuse")
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t-kanban")
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "123")
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "planspec")
        monkeypatch.setenv("HERMES_PROFILE", "coder")
        propagated = {}

        class _Attributes:
            def __enter__(self):
                return None

            def __exit__(self, *_args):
                return False

        def fake_propagate_attributes(**kwargs):
            propagated.update(kwargs)
            return _Attributes()

        monkeypatch.setattr(mod, "propagate_attributes", fake_propagate_attributes)
        recorded = self._start_root_trace(mod)

        assert recorded["metadata"] == {
            "source": "hermes", "task_id": "turn-task", "turn_id": "",
            "api_request_id": "", "platform": "cli", "provider": "provider",
            "model": "model", "api_mode": "chat", "kanban_task_id": "t-kanban",
            "kanban_run_id": "123", "kanban_board": "planspec", "kanban_profile": "coder",
        }
        assert propagated["tags"] == ["hermes", "langfuse", "kanban-worker"]
        # Regression: kanban identity must be propagated to TRACE level,
        # not just stamped on the root span.  Without metadata= in
        # propagate_attributes(), Langfuse v4 creates the trace with
        # empty metadata and the exporter can never match it.
        assert propagated["metadata"] == {
            "kanban_task_id": "t-kanban",
            "kanban_run_id": "123",
            "kanban_board": "planspec",
            "kanban_profile": "coder",
        }

    def test_root_trace_payload_is_unchanged_outside_kanban_worker(self, monkeypatch):
        mod = importlib.import_module("plugins.observability.langfuse")
        for name in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_BOARD", "HERMES_PROFILE"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(mod, "propagate_attributes", None)

        recorded = self._start_root_trace(mod)

        assert recorded["metadata"] == {
            "source": "hermes", "task_id": "turn-task", "turn_id": "",
            "api_request_id": "", "platform": "cli", "provider": "provider",
            "model": "model", "api_mode": "chat",
        }

    def test_propagated_metadata_absent_outside_kanban_worker(self, monkeypatch):
        """Non-worker traces must not receive a metadata kwarg at all."""
        mod = importlib.import_module("plugins.observability.langfuse")
        for name in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_BOARD", "HERMES_PROFILE"):
            monkeypatch.delenv(name, raising=False)
        propagated = {}

        class _Attributes:
            def __enter__(self):
                return None

            def __exit__(self, *_args):
                return False

        def fake_propagate_attributes(**kwargs):
            propagated.update(kwargs)
            return _Attributes()

        monkeypatch.setattr(mod, "propagate_attributes", fake_propagate_attributes)
        self._start_root_trace(mod)

        # metadata key must be completely absent, not merely None
        assert "metadata" not in propagated
        assert "kanban-worker" not in propagated.get("tags", [])
        # Exact kwargs for non-worker: session_id, trace_name, tags only
        assert set(propagated.keys()) == {"session_id", "trace_name", "tags"}

    def test_kanban_metadata_env_failure_is_fail_soft(self, monkeypatch):
        mod = importlib.import_module("plugins.observability.langfuse")

        def raise_on_get(*_args, **_kwargs):
            raise RuntimeError("synthetic environment failure")

        monkeypatch.setattr(mod.os.environ, "get", raise_on_get)
        assert mod._kanban_worker_metadata() == {}


@pytest.fixture()
def real_propagate_attributes():
    try:
        from langfuse import propagate_attributes as real_pa
    except ImportError:
        pytest.skip("langfuse SDK not installed")
    return real_pa


class TestPropagateAttributesSDKContract:
    """Verify the real Langfuse SDK contract for propagate_attributes(metadata=...).

    The regression tests above use a **kwargs fake that accepts any keyword.
    This class pins the actual SDK signature so an incompatible SDK upgrade
    is caught at test time rather than silently failing in production.
    """

    def test_metadata_is_keyword_only_param(self, real_propagate_attributes):
        """propagate_attributes must accept metadata as a keyword-only arg."""
        import inspect

        sig = inspect.signature(real_propagate_attributes)
        params = sig.parameters
        assert "metadata" in params, (
            f"propagate_attributes signature lacks 'metadata': {sig}"
        )
        meta_param = params["metadata"]
        assert meta_param.kind == inspect.Parameter.KEYWORD_ONLY, (
            f"'metadata' must be KEYWORD_ONLY, got {meta_param.kind.name}"
        )
        assert meta_param.default is None, (
            f"'metadata' default must be None, got {meta_param.default!r}"
        )

    def test_metadata_kwarg_accepted_at_runtime(self, real_propagate_attributes):
        """Calling propagate_attributes(metadata={...}) must not raise TypeError."""
        # This exercises the real SDK entry point (context manager creation,
        # not the network export) to prove the kwarg is wired through.
        try:
            ctx = real_propagate_attributes(
                session_id="contract-test",
                trace_name="contract",
                tags=["contract"],
                metadata={"kanban_task_id": "t_test", "kanban_run_id": "1"},
            )
            # Enter and exit the context manager to prove it is well-formed.
            with ctx:
                pass
        except TypeError as exc:
            pytest.fail(f"propagate_attributes rejected metadata kwarg: {exc}")

    def test_non_worker_call_omits_metadata(self, real_propagate_attributes):
        """Non-worker call (no metadata kwarg) must also succeed."""
        try:
            with real_propagate_attributes(
                session_id="contract-test",
                trace_name="contract",
                tags=["contract"],
            ):
                pass
        except TypeError as exc:
            pytest.fail(f"propagate_attributes failed without metadata: {exc}")


class TestPropagateAttributesExportContract:
    """Prove the full SDK export chain: propagate_attributes(metadata=...) →
    LangfuseSpanProcessor.on_start → span attributes langfuse.trace.metadata.*.

    This uses the REAL LangfuseSpanProcessor (with dummy credentials, no
    network) and an InMemorySpanExporter to capture the actual OTel span
    attributes that the Langfuse backend would receive.  This reproduces the
    live failure mode: without propagate_attributes(metadata=...), the
    exporter sees zero kanban metadata on the trace.
    """

    @pytest.fixture()
    def span_capture(self):
        """Set up a TracerProvider with the real LangfuseSpanProcessor and an
        InMemorySpanExporter.  Returns (tracer, exporter) and tears down."""
        try:
            from langfuse._client.span_processor import LangfuseSpanProcessor
        except ImportError:
            pytest.skip("langfuse SDK not installed")
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        # Real LangfuseSpanProcessor with an injected InMemorySpanExporter —
        # no OTLP-HTTP exporter is constructed, no network calls are made.
        # The on_start propagation logic (span_processor.py:147-165) runs
        # identically regardless of the downstream exporter.
        provider.add_span_processor(
            LangfuseSpanProcessor(
                public_key="pk-contract-test",
                secret_key="sk-contract-test-dummy",
                base_url="http://localhost:1",
                span_exporter=InMemorySpanExporter(),
            )
        )
        tracer = provider.get_tracer("contract-test")
        yield tracer, exporter
        provider.shutdown()

    def test_metadata_exported_as_trace_metadata_span_attributes(
        self, span_capture, real_propagate_attributes
    ):
        """Worker case: metadata dict lands as langfuse.trace.metadata.* on
        the exported span — the exact attributes the Langfuse backend reads
        to populate trace-level metadata."""
        tracer, exporter = span_capture

        with real_propagate_attributes(
            session_id="s1",
            trace_name="worker-trace",
            tags=["kanban-worker"],
            metadata={"kanban_task_id": "t_test123", "kanban_run_id": "42"},
        ):
            with tracer.start_as_current_span("root-observation"):
                pass

        spans = exporter.get_finished_spans()
        assert len(spans) == 1, f"Expected 1 span, got {len(spans)}"
        attrs = dict(spans[0].attributes)

        assert attrs.get("langfuse.trace.metadata.kanban_task_id") == "t_test123", (
            f"kanban_task_id missing from exported span attributes: {sorted(attrs)}"
        )
        assert attrs.get("langfuse.trace.metadata.kanban_run_id") == "42", (
            f"kanban_run_id missing from exported span attributes: {sorted(attrs)}"
        )

    def test_non_worker_export_has_no_metadata_attributes(
        self, span_capture, real_propagate_attributes
    ):
        """Non-worker case: no metadata kwarg → no langfuse.trace.metadata.*
        attributes on the exported span."""
        tracer, exporter = span_capture

        with real_propagate_attributes(
            session_id="s1",
            trace_name="non-worker-trace",
            tags=["regular"],
        ):
            with tracer.start_as_current_span("root-observation"):
                pass

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        attrs = dict(spans[0].attributes)

        metadata_keys = [k for k in attrs if k.startswith("langfuse.trace.metadata")]
        assert not metadata_keys, (
            f"Unexpected metadata attributes in non-worker span: {metadata_keys}"
        )


class TestTraceScopeKey:
    def _fresh_plugin(self):
        mod_name = "plugins.observability.langfuse"
        sys.modules.pop(mod_name, None)
        return importlib.import_module(mod_name)

    def test_trace_key_scopes_by_turn_id_when_available(self):
        plugin = self._fresh_plugin()

        key_a = plugin._trace_key("task-1", "session-1", turn_id="turn-a")
        key_b = plugin._trace_key("task-1", "session-1", turn_id="turn-b")

        assert key_a != key_b
        assert "turn:turn-a" in key_a
        assert "turn:turn-b" in key_b

    def test_trace_key_scopes_by_api_request_id_when_turn_missing(self):
        plugin = self._fresh_plugin()

        key_a = plugin._trace_key("task-1", "session-1", api_request_id="req-a")
        key_b = plugin._trace_key("task-1", "session-1", api_request_id="req-b")

        assert key_a != key_b
        assert "api:req-a" in key_a
        assert "api:req-b" in key_b

    def test_trace_key_keeps_legacy_shape_without_turn_or_api_id(self):
        plugin = self._fresh_plugin()
        assert plugin._trace_key("task-1", "session-1") == "task-1"


# ---------------------------------------------------------------------------
# End-to-end collision regression: two turns of ONE gateway session must not
# share trace state.  The helper-level tests above prove _trace_key returns
# distinct keys; this drives the real pre/post hooks to prove the keys are
# actually threaded through so the second turn gets its own root trace.
#
# Gateway reality this reproduces:
#   * task_id == session_id for every turn        (gateway/run.py)
#   * turn_id is unique per turn                   (turn_context.py)
#   * api_call_count resets to 1 each turn         (conversation_loop.py)
#
# Before the turn/request scoping, _trace_key collapsed to the constant
# session_id.  That worked only because _finish_trace pops the key on a clean
# turn end.  When turn 1 does NOT finalize (interrupted, tool-only final step,
# or empty final content), its state lingered under session_id and turn 2
# silently merged into turn 1's trace instead of opening its own.
# ---------------------------------------------------------------------------


class TestTurnTraceIsolation:
    def _fresh_plugin(self):
        sys.modules.pop("plugins.observability.langfuse", None)
        return importlib.import_module("plugins.observability.langfuse")

    @staticmethod
    def _fake_client(started):
        """A minimal Langfuse stand-in that records each root trace opened.

        ``_start_root_trace`` calls ``create_trace_id`` then opens a root via
        ``start_as_current_observation(...)`` (a context manager whose
        ``__enter__`` returns the root span).  We record one entry per root
        actually opened so the test can count distinct traces.
        """

        class _Span:
            def update(self, **kw):
                pass

            def end(self, **kw):
                pass

            def set_trace_io(self, **kw):
                pass

            def start_observation(self, **kw):
                return _Span()

        class _RootCM:
            def __enter__(self):
                return _Span()

            def __exit__(self, *exc):
                return False

        class _Client:
            def create_trace_id(self, seed=None):
                return f"trace::{seed}"

            def start_as_current_observation(self, **kw):
                started.append(kw.get("trace_context", {}).get("trace_id"))
                return _RootCM()

            def flush(self):
                pass

        return _Client()

    def _run_turn(self, mod, *, session, turn_n, finalize):
        """Drive one turn through the request-scoped hooks the gateway fires."""
        task_id = session  # gateway sets task_id == session_id
        turn_id = f"{session}:{task_id}:turn{turn_n}"
        api_call_count = 1  # resets every turn
        api_request_id = f"{turn_id}:api:{api_call_count}"

        mod.on_pre_llm_request(
            task_id=task_id,
            session_id=session,
            model="m",
            provider="p",
            api_mode="chat",
            api_call_count=api_call_count,
            request_messages=[{"role": "user", "content": "hi"}],
            turn_id=turn_id,
            api_request_id=api_request_id,
        )
        # finalize=False => leave a tool call on the final response so
        # _finish_trace is skipped and the turn's state lingers.
        mod.on_post_llm_call(
            task_id=task_id,
            session_id=session,
            model="m",
            provider="p",
            api_mode="chat",
            api_call_count=api_call_count,
            assistant_content_chars=5 if finalize else 0,
            assistant_tool_call_count=0 if finalize else 1,
            usage={"input_tokens": 10, "output_tokens": 5},
            turn_id=turn_id,
            api_request_id=api_request_id,
        )

    def test_unfinalized_turn_does_not_capture_next_turn(self, monkeypatch):
        """A turn that never finalizes must not absorb the following turn."""
        mod = self._fresh_plugin()
        started: list = []
        monkeypatch.setattr(mod, "_get_langfuse", lambda: self._fake_client(started))
        monkeypatch.setattr(mod, "_end_observation", lambda *a, **k: None)
        mod._TRACE_STATE.clear()

        # Turn 1 ends without finalizing (its final step still has a tool call).
        self._run_turn(mod, session="sess-iso", turn_n=1, finalize=False)
        # Turn 2 is a normal, fully finalizing turn in the SAME session.
        self._run_turn(mod, session="sess-iso", turn_n=2, finalize=True)

        # Each turn opened its OWN root trace.  On the pre-fix code the second
        # turn reused turn 1's lingering state and only one trace was opened.
        assert len(started) == 2

        # Turn 2 finalized and was popped by _finish_trace; only turn 1's
        # (non-finalizing) state lingers.  Assert the surviving key is turn 1's
        # and that turn 2 never merged into it — `all(...)` over an empty set
        # would pass vacuously, so pin the exact surviving key instead.
        keys = list(mod._TRACE_STATE.keys())
        assert len(keys) == 1
        assert "turn1" in keys[0]
        assert "turn2" not in keys[0]

    def test_pre_and_post_hooks_share_one_key_within_a_turn(self, monkeypatch):
        """turn_id is preferred over api_request_id so the turn-scoped
        post_llm_call (which carries no api_request_id) still resolves to the
        same key as the request-scoped pre/post_api_request hooks.  If the
        ordering were reversed, finalization would silently break."""
        mod = self._fresh_plugin()
        turn_id = "S:T:turnX"
        api_request_id = f"{turn_id}:api:1"

        k_pre_api = mod._trace_key("T", "S", turn_id=turn_id, api_request_id=api_request_id)
        k_post_api = mod._trace_key("T", "S", turn_id=turn_id, api_request_id=api_request_id)
        k_post_turn = mod._trace_key("T", "S", turn_id=turn_id, api_request_id="")

        assert k_pre_api == k_post_api == k_post_turn

    def test_non_finalizing_turns_do_not_grow_state_unboundedly(self, monkeypatch):
        """Per-turn keys mean a turn that never finalizes leaves a lingering
        entry.  Without a cap that grows once per non-finalizing turn forever;
        the LRU eviction must bound _TRACE_STATE at _MAX_TRACE_STATE.
        """
        mod = self._fresh_plugin()
        started: list = []
        monkeypatch.setattr(mod, "_get_langfuse", lambda: self._fake_client(started))
        monkeypatch.setattr(mod, "_end_observation", lambda *a, **k: None)
        monkeypatch.setattr(mod, "_MAX_TRACE_STATE", 8)
        mod._TRACE_STATE.clear()

        # Far more non-finalizing turns than the cap.
        for n in range(50):
            self._run_turn(mod, session="sess-leak", turn_n=n, finalize=False)

        assert len(mod._TRACE_STATE) <= 8
        # The survivors are the most-recently-updated turns (LRU eviction).
        surviving = sorted(int(k.rsplit("turn", 1)[1]) for k in mod._TRACE_STATE)
        assert surviving == list(range(42, 50))

    def test_trace_key_strings_unchanged_by_refactor(self):
        """Pin the exact key strings across all task/session/turn/api
        combinations so the _scope_prefix extraction can never silently change
        a key (keys are matched across hooks; a drift breaks finalization)."""
        mod = self._fresh_plugin()
        tk = mod._trace_key
        assert tk("t", "s", turn_id="u") == "task:t:turn:u"
        assert tk("", "s", turn_id="u") == "session:s:turn:u"
        assert tk("t", "s", api_request_id="r") == "task:t:api:r"
        assert tk("", "s", api_request_id="r") == "session:s:api:r"
        assert tk("t", "s") == "t"                       # legacy: bare task_id
        assert tk("", "s") == "session:s"
        # turn_id wins over api_request_id when both are present.
        assert tk("t", "s", turn_id="u", api_request_id="r") == "task:t:turn:u"


# ---------------------------------------------------------------------------
# Placeholder-credential guard (#23823).
#
# Regression coverage for the silent-failure bug: when an operator leaves
# HERMES_LANGFUSE_PUBLIC_KEY / SECRET_KEY at a template value like
# "placeholder", "test-key", or "your-langfuse-key", the SDK accepts the
# credentials at construction time (it does no server-side validation
# eagerly) but drops every trace at flush time, with no signal in the
# Hermes logs.  The fix in `_get_langfuse()` validates the documented
# `pk-lf-` / `sk-lf-` prefix Langfuse always issues, surfaces a one-shot
# warning naming the offending env var(s), and short-circuits via the
# same `_INIT_FAILED` path used for missing credentials so subsequent
# hook invocations don't re-log.
# ---------------------------------------------------------------------------


class _FakeLangfuse:
    """Stand-in for the real :class:`langfuse.Langfuse` so tests don't
    need the optional ``langfuse`` SDK installed.  The plugin's runtime
    gate refuses to proceed past ``if Langfuse is None`` when the SDK
    is missing, which would short-circuit before the placeholder check
    can fire.  Patching ``plugin.Langfuse`` with this class lets the
    placeholder validator exercise its full code path."""

    instances: list["_FakeLangfuse"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        _FakeLangfuse.instances.append(self)


class TestPlaceholderKeyDetection:
    LOGGER_NAME = "plugins.observability.langfuse"

    def _fresh_plugin(self, monkeypatch=None):
        mod_name = "plugins.observability.langfuse"
        sys.modules.pop(mod_name, None)
        mod = importlib.import_module(mod_name)
        if monkeypatch is not None:
            # Pretend the SDK is installed so `_get_langfuse()` actually
            # reaches the placeholder check.  Real SDK calls are never
            # made because the placeholder/missing-credentials paths
            # return before constructing a client.
            _FakeLangfuse.instances.clear()
            monkeypatch.setattr(mod, "Langfuse", _FakeLangfuse, raising=False)
        return mod

    @staticmethod
    def _clear_env(monkeypatch):
        for k in (
            "HERMES_LANGFUSE_PUBLIC_KEY", "HERMES_LANGFUSE_SECRET_KEY",
            "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY",
        ):
            monkeypatch.delenv(k, raising=False)

    # -- helper unit tests (no SDK stub needed: these don't go through
    #    _get_langfuse, they exercise the pure-Python helpers directly) ------

    def test_redact_key_preview_empty(self, monkeypatch):
        self._clear_env(monkeypatch)
        plugin = self._fresh_plugin()
        assert plugin._redact_key_preview("") == "<empty>"

    def test_redact_key_preview_short_value_echoed(self, monkeypatch):
        """Short placeholder strings are echoed in full so the operator
        can see exactly which template they forgot to replace."""
        self._clear_env(monkeypatch)
        plugin = self._fresh_plugin()
        assert plugin._redact_key_preview("placeholder") == "'placeholder'"
        assert plugin._redact_key_preview("test-key") == "'test-key'"

    def test_redact_key_preview_long_value_truncated(self, monkeypatch):
        """If an operator pasted a real secret into the wrong env var the
        preview must NOT echo it in full — only the leading 6 chars."""
        self._clear_env(monkeypatch)
        plugin = self._fresh_plugin()
        result = plugin._redact_key_preview("sk-lf-abcdefghijklmnop")
        assert "abcdefghij" not in result
        assert result.startswith("'sk-lf-")
        assert result.endswith("...'")

    def test_validate_langfuse_key_accepts_documented_prefix(self, monkeypatch):
        self._clear_env(monkeypatch)
        plugin = self._fresh_plugin()
        assert plugin._validate_langfuse_key(
            "HERMES_LANGFUSE_PUBLIC_KEY", "pk-lf-real-public-xyz"
        ) is None
        assert plugin._validate_langfuse_key(
            "HERMES_LANGFUSE_SECRET_KEY", "sk-lf-real-secret-xyz"
        ) is None

    def test_validate_langfuse_key_rejects_wrong_prefix(self, monkeypatch):
        self._clear_env(monkeypatch)
        plugin = self._fresh_plugin()
        msg = plugin._validate_langfuse_key(
            "HERMES_LANGFUSE_PUBLIC_KEY", "placeholder"
        )
        assert msg is not None
        assert "HERMES_LANGFUSE_PUBLIC_KEY" in msg
        assert "pk-lf-" in msg

    def test_validate_langfuse_key_unknown_name_passes(self, monkeypatch):
        """Defensive: an env var with no registered prefix is trusted."""
        self._clear_env(monkeypatch)
        plugin = self._fresh_plugin()
        assert plugin._validate_langfuse_key("HERMES_LANGFUSE_BASE_URL", "anything") is None

    # -- end-to-end _get_langfuse() behaviour --------------------------------
    # These tests pass `monkeypatch` to _fresh_plugin() so the helper can
    # stub out `Langfuse` (the optional SDK).  Without that, every call
    # short-circuits at `if Langfuse is None` before reaching the
    # placeholder validator — masking the very behaviour we're testing.

    def test_placeholder_public_key_warns_and_skips(self, monkeypatch, caplog):
        self._clear_env(monkeypatch)
        monkeypatch.setenv("HERMES_LANGFUSE_PUBLIC_KEY", "placeholder")
        monkeypatch.setenv("HERMES_LANGFUSE_SECRET_KEY", "sk-lf-real-secret-xyz")
        plugin = self._fresh_plugin(monkeypatch)
        with caplog.at_level(logging.WARNING, logger=self.LOGGER_NAME):
            assert plugin._get_langfuse() is None
        text = caplog.text
        assert "HERMES_LANGFUSE_PUBLIC_KEY" in text
        assert "'placeholder'" in text
        assert "pk-lf-" in text
        # The valid secret value must NOT appear (the var NAME does, in
        # the "or unset ..." hint, but the value preview shouldn't).
        assert "'sk-lf-" not in text
        # Never constructed the SDK client — short-circuited before that.
        assert _FakeLangfuse.instances == []

    def test_placeholder_secret_key_warns_and_skips(self, monkeypatch, caplog):
        self._clear_env(monkeypatch)
        monkeypatch.setenv("HERMES_LANGFUSE_PUBLIC_KEY", "pk-lf-real-public-xyz")
        monkeypatch.setenv("HERMES_LANGFUSE_SECRET_KEY", "test-key")
        plugin = self._fresh_plugin(monkeypatch)
        with caplog.at_level(logging.WARNING, logger=self.LOGGER_NAME):
            assert plugin._get_langfuse() is None
        text = caplog.text
        assert "HERMES_LANGFUSE_SECRET_KEY" in text
        assert "'test-key'" in text
        assert "sk-lf-" in text
        # The valid public value must NOT appear.
        assert "'pk-lf-" not in text
        assert _FakeLangfuse.instances == []

    def test_both_placeholders_one_warning_with_both_keys(self, monkeypatch, caplog):
        self._clear_env(monkeypatch)
        monkeypatch.setenv("HERMES_LANGFUSE_PUBLIC_KEY", "placeholder")
        monkeypatch.setenv("HERMES_LANGFUSE_SECRET_KEY", "placeholder")
        plugin = self._fresh_plugin(monkeypatch)
        with caplog.at_level(logging.WARNING, logger=self.LOGGER_NAME):
            assert plugin._get_langfuse() is None
        warnings = [r for r in caplog.records if r.levelname == "WARNING"
                    and r.name == self.LOGGER_NAME]
        assert len(warnings) == 1, (
            f"Expected a single combined warning; got {len(warnings)}:\n"
            + "\n".join(r.getMessage() for r in warnings)
        )
        text = warnings[0].getMessage()
        assert "HERMES_LANGFUSE_PUBLIC_KEY" in text
        assert "HERMES_LANGFUSE_SECRET_KEY" in text

    def test_repeated_calls_do_not_re_warn(self, monkeypatch, caplog):
        """The cached ``_INIT_FAILED`` sentinel must short-circuit
        subsequent calls so each hook invocation isn't a fresh log
        line — otherwise a busy gateway will spam the operator's
        terminal."""
        self._clear_env(monkeypatch)
        monkeypatch.setenv("HERMES_LANGFUSE_PUBLIC_KEY", "placeholder")
        monkeypatch.setenv("HERMES_LANGFUSE_SECRET_KEY", "placeholder")
        plugin = self._fresh_plugin(monkeypatch)
        with caplog.at_level(logging.WARNING, logger=self.LOGGER_NAME):
            for _ in range(15):
                assert plugin._get_langfuse() is None
        warnings = [r for r in caplog.records if r.levelname == "WARNING"
                    and r.name == self.LOGGER_NAME]
        assert len(warnings) == 1, (
            f"Warning fired {len(warnings)} times across 15 calls; "
            "expected 1 (cached via _INIT_FAILED)"
        )

    @pytest.mark.parametrize("placeholder", [
        "placeholder",
        "test-key",
        "your-langfuse-key",
        "change-me",
        "xxx",
        "dummy-key-here",
        "<your-key>",
        "REPLACE_ME",
    ])
    def test_common_placeholders_detected(self, monkeypatch, caplog, placeholder):
        """A grab-bag of values that real-world ``.env.example`` templates
        use as stand-ins.  Any of them in either key must trip the guard."""
        self._clear_env(monkeypatch)
        monkeypatch.setenv("HERMES_LANGFUSE_PUBLIC_KEY", placeholder)
        monkeypatch.setenv("HERMES_LANGFUSE_SECRET_KEY", "sk-lf-real-secret-xyz")
        plugin = self._fresh_plugin(monkeypatch)
        with caplog.at_level(logging.WARNING, logger=self.LOGGER_NAME):
            assert plugin._get_langfuse() is None
        assert "HERMES_LANGFUSE_PUBLIC_KEY" in caplog.text

    def test_legacy_LANGFUSE_PUBLIC_KEY_also_validated(self, monkeypatch, caplog):
        """The plugin reads both the canonical HERMES_-prefixed env var and
        the legacy bare ``LANGFUSE_PUBLIC_KEY``.  The validator must run on
        whichever value ``_get_langfuse()`` actually consumed."""
        self._clear_env(monkeypatch)
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "placeholder")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-real-secret-xyz")
        plugin = self._fresh_plugin(monkeypatch)
        with caplog.at_level(logging.WARNING, logger=self.LOGGER_NAME):
            assert plugin._get_langfuse() is None
        # Warning names the canonical user-facing env var (the bare
        # LANGFUSE_PUBLIC_KEY is a backwards-compat alias for the
        # HERMES_-prefixed one — operators set the HERMES_-prefixed one).
        assert "HERMES_LANGFUSE_PUBLIC_KEY" in caplog.text
        assert "'placeholder'" in caplog.text

    def test_missing_credentials_still_skip_silently(self, monkeypatch, caplog):
        """Missing-creds is the documented opt-out path (operator hasn't
        configured the plugin yet) — it must remain SILENT.  Regression
        guard against the placeholder validator accidentally running on
        empty values and re-introducing log noise for unconfigured
        installs."""
        self._clear_env(monkeypatch)
        plugin = self._fresh_plugin(monkeypatch)
        with caplog.at_level(logging.WARNING, logger=self.LOGGER_NAME):
            assert plugin._get_langfuse() is None
        warnings = [r for r in caplog.records if r.levelname == "WARNING"
                    and r.name == self.LOGGER_NAME]
        assert warnings == []

    def test_sdk_not_installed_still_skips_silently(self, monkeypatch, caplog):
        """If the langfuse SDK isn't installed at all, the placeholder
        check should never run — there's nothing the operator can do
        about a credential mismatch when the package is missing, and
        re-warning here would dilute the actually-actionable SDK-missing
        signal upstream.  The ``Langfuse is None`` guard at the top of
        ``_get_langfuse`` already handles this; this test pins that
        behaviour."""
        self._clear_env(monkeypatch)
        monkeypatch.setenv("HERMES_LANGFUSE_PUBLIC_KEY", "placeholder")
        monkeypatch.setenv("HERMES_LANGFUSE_SECRET_KEY", "placeholder")
        # NO monkeypatch on Langfuse here — falls back to whatever the
        # plugin imported at module load (None if SDK absent).
        plugin = self._fresh_plugin()
        monkeypatch.setattr(plugin, "Langfuse", None, raising=False)
        with caplog.at_level(logging.WARNING, logger=self.LOGGER_NAME):
            assert plugin._get_langfuse() is None
        warnings = [r for r in caplog.records if r.levelname == "WARNING"
                    and r.name == self.LOGGER_NAME]
        assert warnings == []

    def test_valid_prefixes_do_not_trigger_placeholder_warning(self, monkeypatch, caplog):
        """Real Langfuse keys (``pk-lf-…`` / ``sk-lf-…``) must pass the
        guard and proceed to SDK init.  We stub the SDK constructor with
        a recording fake so the assertion can confirm BOTH that the
        placeholder warning didn't fire AND that the client was actually
        constructed — the latter is the success signal the bug report
        wanted."""
        self._clear_env(monkeypatch)
        monkeypatch.setenv("HERMES_LANGFUSE_PUBLIC_KEY", "pk-lf-real-public-xyz")
        monkeypatch.setenv("HERMES_LANGFUSE_SECRET_KEY", "sk-lf-real-secret-xyz")
        plugin = self._fresh_plugin(monkeypatch)
        with caplog.at_level(logging.WARNING, logger=self.LOGGER_NAME):
            client = plugin._get_langfuse()
        assert isinstance(client, _FakeLangfuse)
        assert client.kwargs["public_key"] == "pk-lf-real-public-xyz"
        assert client.kwargs["secret_key"] == "sk-lf-real-secret-xyz"
        assert "placeholders" not in caplog.text.lower(), (
            f"Valid Langfuse keys tripped the placeholder guard: {caplog.text!r}"
        )


class TestRequestMessageCoercion:
    def test_prefers_request_messages_then_messages_then_history_then_user_message(self):
        sys.modules.pop("plugins.observability.langfuse", None)
        mod = importlib.import_module("plugins.observability.langfuse")

        assert mod._coerce_request_messages(
            request_messages=[{"role": "system", "content": "s"}],
            messages=[{"role": "user", "content": "m"}],
            conversation_history=[{"role": "user", "content": "h"}],
            user_message="u",
        ) == [{"role": "system", "content": "s"}]
        assert mod._coerce_request_messages(
            messages=[{"role": "user", "content": "m"}],
            conversation_history=[{"role": "user", "content": "h"}],
            user_message="u",
        ) == [{"role": "user", "content": "m"}]
        assert mod._coerce_request_messages(
            conversation_history=[{"role": "user", "content": "h"}],
            user_message="u",
        ) == [{"role": "user", "content": "h"}]
        assert mod._coerce_request_messages(user_message="u") == [{"role": "user", "content": "u"}]


class TestToolCallOutputBackfill:
    def test_post_tool_call_backfills_matching_turn_tool_call_output(self, monkeypatch):
        sys.modules.pop("plugins.observability.langfuse", None)
        mod = importlib.import_module("plugins.observability.langfuse")

        observation = object()
        state = mod.TraceState(trace_id="trace-1", root_ctx=None, root_span=None)
        state.tools["call-1"] = observation
        state.turn_tool_calls.append({
            "id": "call-1",
            "type": "function",
            "name": "web_extract",
            "arguments": '{"urls": ["https://example.com"]}',
            "function": {
                "name": "web_extract",
                "arguments": '{"urls": ["https://example.com"]}',
            },
        })

        task_key = mod._trace_key("task-1", "session-1")
        monkeypatch.setitem(mod._TRACE_STATE, task_key, state)

        ended = {}

        def fake_end_observation(obs, *, output=None, metadata=None, usage_details=None, cost_details=None):
            ended["observation"] = obs
            ended["output"] = output
            ended["metadata"] = metadata

        monkeypatch.setattr(mod, "_end_observation", fake_end_observation)

        mod.on_post_tool_call(
            tool_name="web_extract",
            args={"urls": ["https://example.com"]},
            result='{"results": [{"url": "https://example.com", "content": "Example Domain"}]}',
            task_id="task-1",
            session_id="session-1",
            tool_call_id="call-1",
        )

        assert ended["observation"] is observation
        assert state.turn_tool_calls[0]["output"] == ended["output"]
        assert state.turn_tool_calls[0]["function"]["output"] == ended["output"]
        assert state.turn_tool_calls[0]["output"] == mod._redact_raw_content({
            "results": [{"url": "https://example.com", "content": "Example Domain"}]
        })

    def test_serialize_messages_keeps_tool_name_and_call_id(self):
        sys.modules.pop("plugins.observability.langfuse", None)
        mod = importlib.import_module("plugins.observability.langfuse")

        messages = [{
            "role": "tool",
            "name": "web_extract",
            "tool_call_id": "call-1",
            "content": '{"ok": true}',
        }]

        assert mod._serialize_messages(messages) == [{
            "role": "tool",
            "name": "web_extract",
            "tool_call_id": "call-1",
            "content": mod._redact_raw_content('{"ok": true}'),
        }]

    def test_serialize_tool_calls_emits_openai_style_function_shape(self):
        sys.modules.pop("plugins.observability.langfuse", None)
        mod = importlib.import_module("plugins.observability.langfuse")

        class _Fn:
            name = "web_extract"
            arguments = '{"urls": ["https://example.com"]}'

        class _ToolCall:
            id = "call-1"
            type = "function"
            function = _Fn()

        assert mod._serialize_tool_calls([_ToolCall()]) == [{
            "id": "call-1",
            "type": "function",
            "name": "web_extract",
            "arguments": mod._redact_raw_content('{"urls": ["https://example.com"]}'),
            "function": {
                "name": "web_extract",
                "arguments": mod._redact_raw_content('{"urls": ["https://example.com"]}'),
            },
        }]


class TestToolObservationKeying:
    """Tests for pre/post tool_call observation matching when tool_call_id is absent."""

    def _make_mod(self):
        sys.modules.pop("plugins.observability.langfuse", None)
        return importlib.import_module("plugins.observability.langfuse")

    def test_empty_tool_call_id_single_tool_sets_output(self, monkeypatch):
        mod = self._make_mod()
        obs = object()
        state = mod.TraceState(trace_id="t", root_ctx=None, root_span=None)
        state.pending_tools_by_name.setdefault("my_tool", []).append(obs)

        task_key = mod._trace_key("task-1", "sess-1")
        monkeypatch.setitem(mod._TRACE_STATE, task_key, state)

        ended = {}

        def fake_end(o, *, output=None, metadata=None, **kw):
            ended["obs"] = o
            ended["output"] = output

        monkeypatch.setattr(mod, "_end_observation", fake_end)

        mod.on_post_tool_call(
            tool_name="my_tool",
            args={},
            result='{"ok": true}',
            task_id="task-1",
            session_id="sess-1",
            tool_call_id="",
        )

        assert ended["obs"] is obs
        assert ended["output"] == mod._redact_raw_content({"ok": True})
        assert state.pending_tools_by_name.get("my_tool") is None

    def test_empty_tool_call_id_observations_are_fifo_within_tool_name(self, monkeypatch):
        """Two queued observations are consumed in FIFO order so the first
        post hook gets the first observation's output, not the second.

        Sequential-on-one-thread coverage; the real concurrent case is
        guarded by ``_STATE_LOCK`` around every read-modify-write on
        ``pending_tools_by_name`` and is exercised in
        ``test_threaded_post_calls_preserve_fifo_under_lock`` below.
        """
        mod = self._make_mod()
        obs_a, obs_b = object(), object()
        state = mod.TraceState(trace_id="t", root_ctx=None, root_span=None)
        state.pending_tools_by_name["web_extract"] = [obs_a, obs_b]

        task_key = mod._trace_key("task-1", "sess-1")
        monkeypatch.setitem(mod._TRACE_STATE, task_key, state)

        calls = []

        def fake_end(o, *, output=None, metadata=None, **kw):
            calls.append((o, output))

        monkeypatch.setattr(mod, "_end_observation", fake_end)

        mod.on_post_tool_call(
            tool_name="web_extract", args={}, result='{"val": "a"}',
            task_id="task-1", session_id="sess-1", tool_call_id="",
        )
        mod.on_post_tool_call(
            tool_name="web_extract", args={}, result='{"val": "b"}',
            task_id="task-1", session_id="sess-1", tool_call_id="",
        )

        assert calls[0] == (obs_a, mod._redact_raw_content({"val": "a"}))
        assert calls[1] == (obs_b, mod._redact_raw_content({"val": "b"}))
        assert state.pending_tools_by_name.get("web_extract") is None

    def test_threaded_post_calls_preserve_fifo_under_lock(self, monkeypatch):
        """The actual concurrency contract: when 8 threads race to drain
        the pending queue, no observation is consumed twice and none is
        lost.  Validates ``_STATE_LOCK`` discipline, not Python list
        semantics."""
        import threading

        mod = self._make_mod()
        n = 8
        observations = [object() for _ in range(n)]
        state = mod.TraceState(trace_id="t", root_ctx=None, root_span=None)
        state.pending_tools_by_name["web_extract"] = list(observations)

        task_key = mod._trace_key("task-thr", "sess-thr")
        monkeypatch.setitem(mod._TRACE_STATE, task_key, state)

        recorded: list = []
        lock = threading.Lock()

        def fake_end(o, *, output=None, metadata=None, **kw):
            with lock:
                recorded.append(o)

        monkeypatch.setattr(mod, "_end_observation", fake_end)

        barrier = threading.Barrier(n)

        def worker():
            barrier.wait()
            mod.on_post_tool_call(
                tool_name="web_extract", args={}, result='{"ok": true}',
                task_id="task-thr", session_id="sess-thr", tool_call_id="",
            )

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Every observation was consumed exactly once; queue is empty.
        assert len(recorded) == n
        assert set(map(id, recorded)) == set(map(id, observations))
        assert state.pending_tools_by_name.get("web_extract") is None

    def test_explicit_tool_call_id_uses_tools_dict(self, monkeypatch):
        """When tool_call_id is present, pending_tools_by_name is not touched."""
        mod = self._make_mod()
        obs = object()
        state = mod.TraceState(trace_id="t", root_ctx=None, root_span=None)
        state.tools["call-99"] = obs

        task_key = mod._trace_key("task-1", "sess-1")
        monkeypatch.setitem(mod._TRACE_STATE, task_key, state)

        ended = {}

        def fake_end(o, *, output=None, metadata=None, **kw):
            ended["obs"] = o
            ended["output"] = output

        monkeypatch.setattr(mod, "_end_observation", fake_end)

        mod.on_post_tool_call(
            tool_name="my_tool", args={}, result='{"status": "done"}',
            task_id="task-1", session_id="sess-1", tool_call_id="call-99",
        )

        assert ended["obs"] is obs
        assert ended["output"] == mod._redact_raw_content({"status": "done"})
        assert not state.tools


class TestPrivacyContract:
    def test_raw_prompt_output_and_tool_arguments_are_never_serialized(self):
        mod = importlib.import_module("plugins.observability.langfuse")
        sentinel = "sk-test-never-export-this"

        payload = {
            "messages": mod._serialize_messages([{"role": "user", "content": sentinel}]),
            "tool_calls": mod._serialize_tool_calls([type("Call", (), {
                "id": "call-1", "type": "function",
                "function": type("Function", (), {"name": "read_file", "arguments": sentinel})(),
            })()]),
            "output": mod._redact_raw_content(sentinel),
        }

        assert sentinel not in repr(payload)
        assert payload["messages"][0]["content"]["omitted"] is True
        assert payload["tool_calls"][0]["arguments"]["reason"] == "raw_content_not_exported"

    def test_redaction_is_fail_closed_for_unclassified_values(self):
        mod = importlib.import_module("plugins.observability.langfuse")
        assert mod._redact_raw_content({"unclassified": "private"}) == {
            "omitted": True,
            "reason": "raw_content_not_exported",
            "type": "dict",
            "length": 1,
        }

    def test_root_trace_completion_never_exports_raw_output(self, monkeypatch):
        mod = importlib.import_module("plugins.observability.langfuse")
        sentinel = "sk-root-output-must-not-export"
        exported = {}

        class RootSpan:
            def set_trace_io(self, **kwargs):
                exported["trace_io"] = kwargs

            def update(self, **kwargs):
                exported["update"] = kwargs

            def end(self):
                exported["ended"] = True

        class Client:
            def flush(self):
                exported["flushed"] = True

        task_key = "privacy-root-output"
        monkeypatch.setattr(mod, "_get_langfuse", lambda: Client())
        monkeypatch.setitem(
            mod._TRACE_STATE,
            task_key,
            mod.TraceState(trace_id="trace", root_ctx=None, root_span=RootSpan()),
        )

        mod._finish_trace(task_key, output={"content": sentinel})

        assert sentinel not in repr(exported)
        assert exported["trace_io"]["output"]["reason"] == "raw_content_not_exported"
        assert exported["update"]["output"]["type"] == "dict"
        assert exported["ended"] and exported["flushed"]


class TestUsageFromSanitizedResponse:
    """Regression: ``post_api_request`` delivers ``response`` as a sanitized
    dict (no ``.usage`` attribute) plus a separate ``usage`` summary dict. The
    post-call handler must read the ``usage`` dict instead of treating the dict
    response as a usage-bearing object and dropping all token/cost data."""

    def _setup(self, mod, monkeypatch):
        # Active client so on_post_llm_call does not early-return.
        monkeypatch.setattr(mod, "_get_langfuse", lambda: object())
        observation = object()
        state = mod.TraceState(trace_id="trace-1", root_ctx=None, root_span=None)
        state.generations[mod._request_key(1)] = observation
        monkeypatch.setitem(mod._TRACE_STATE, mod._trace_key("task-1", "session-1"), state)
        captured = {}

        def fake_end_observation(obs, *, output=None, metadata=None, usage_details=None, cost_details=None):
            captured["usage_details"] = usage_details

        monkeypatch.setattr(mod, "_end_observation", fake_end_observation)
        return captured

    def test_sanitized_dict_response_uses_usage_dict(self, monkeypatch):
        sys.modules.pop("plugins.observability.langfuse", None)
        mod = importlib.import_module("plugins.observability.langfuse")
        captured = self._setup(mod, monkeypatch)

        # A plain dict has no ``.usage`` attribute — mirrors post_api_request.
        mod.on_post_llm_call(
            task_id="task-1",
            session_id="session-1",
            api_call_count=1,
            model="gemini-3-flash-preview",
            response={"model": "gemini-3-flash-preview", "usage": {"input_tokens": 100, "output_tokens": 20}},
            usage={"input_tokens": 100, "output_tokens": 20},
            assistant_content_chars=42,
        )

        # Before the fix the dict response shadowed the usage dict and tokens
        # were lost (usage_details == {}).
        assert captured["usage_details"] == {"input": 100, "output": 20}

    def test_real_response_object_with_usage_still_used(self, monkeypatch):
        sys.modules.pop("plugins.observability.langfuse", None)
        mod = importlib.import_module("plugins.observability.langfuse")
        captured = self._setup(mod, monkeypatch)

        # A response object that genuinely carries usage must still take the
        # response-object path (post_llm_call / legacy behavior).
        seen = {}

        def fake_usage_and_cost(resp, **_):
            seen["resp"] = resp
            return {"input": 7, "output": 3}, {}

        monkeypatch.setattr(mod, "_usage_and_cost", fake_usage_and_cost)

        class _Resp:
            usage = {"prompt_tokens": 7, "completion_tokens": 3}

        resp = _Resp()
        mod.on_post_llm_call(
            task_id="task-1",
            session_id="session-1",
            api_call_count=1,
            model="gemini-3-flash-preview",
            response=resp,
            usage={"input_tokens": 999, "output_tokens": 999},
            assistant_content_chars=42,
        )

        assert seen["resp"] is resp
        assert captured["usage_details"] == {"input": 7, "output": 3}
