"""Tests for model_tools.py — function call dispatch, agent-loop interception, legacy toolsets."""

import json
from unittest.mock import ANY, call, patch

import pytest

from model_tools import (
    handle_function_call,
    get_tool_definitions,
    get_all_tool_names,
    get_toolset_for_tool,
    _AGENT_LOOP_TOOLS,
    _LEGACY_TOOLSET_MAP,
    TOOL_TO_TOOLSET_MAP,
)


# =========================================================================
# handle_function_call
# =========================================================================

class TestHandleFunctionCall:
    def test_agent_loop_tool_returns_error(self):
        for tool_name in _AGENT_LOOP_TOOLS:
            result = json.loads(handle_function_call(tool_name, {}))
            assert "error" in result
            assert "agent loop" in result["error"].lower()

    def test_unknown_tool_returns_error(self):
        result = json.loads(handle_function_call("totally_fake_tool_xyz", {}))
        assert "error" in result
        assert "totally_fake_tool_xyz" in result["error"]

    def test_exception_returns_json_error(self):
        # Even if something goes wrong, should return valid JSON
        result = handle_function_call("web_search", None)  # None args may cause issues
        parsed = json.loads(result)
        assert isinstance(parsed, dict)
        assert "error" in parsed
        assert len(parsed["error"]) > 0
        assert "error" in parsed["error"].lower() or "failed" in parsed["error"].lower()

    def test_tool_hooks_receive_session_and_tool_call_ids(self):
        with (
            patch("model_tools.registry.dispatch", return_value='{"ok":true}'),
            patch("hermes_cli.plugins.invoke_hook") as mock_invoke_hook,
        ):
            result = handle_function_call(
                "web_search",
                {"q": "test"},
                task_id="task-1",
                tool_call_id="call-1",
                session_id="session-1",
            )

        assert result == '{"ok":true}'
        assert mock_invoke_hook.call_args_list == [
            call(
                "pre_tool_call",
                tool_name="web_search",
                args={"q": "test"},
                task_id="task-1",
                session_id="session-1",
                tool_call_id="call-1",
            ),
            call(
                "post_tool_call",
                tool_name="web_search",
                args={"q": "test"},
                result='{"ok":true}',
                task_id="task-1",
                session_id="session-1",
                tool_call_id="call-1",
                duration_ms=ANY,
            ),
            call(
                "transform_tool_result",
                tool_name="web_search",
                args={"q": "test"},
                result='{"ok":true}',
                task_id="task-1",
                session_id="session-1",
                tool_call_id="call-1",
                duration_ms=ANY,
            ),
        ]

    def test_post_tool_call_receives_non_negative_integer_duration_ms(self):
        """Regression: post_tool_call and transform_tool_result hooks must
        receive a non-negative integer ``duration_ms`` kwarg measuring
        dispatch latency.  Inspired by Claude Code 2.1.119, which added
        ``duration_ms`` to its PostToolUse hook inputs.
        """
        with (
            patch("model_tools.registry.dispatch", return_value='{"ok":true}'),
            patch("hermes_cli.plugins.invoke_hook") as mock_invoke_hook,
        ):
            handle_function_call("web_search", {"q": "test"}, task_id="t1")

        kwargs_by_hook = {
            c.args[0]: c.kwargs for c in mock_invoke_hook.call_args_list
        }
        assert "duration_ms" in kwargs_by_hook["post_tool_call"]
        assert "duration_ms" in kwargs_by_hook["transform_tool_result"]

        post_duration = kwargs_by_hook["post_tool_call"]["duration_ms"]
        transform_duration = kwargs_by_hook["transform_tool_result"]["duration_ms"]
        assert isinstance(post_duration, int)
        assert post_duration >= 0
        # Both hooks should observe the same measured duration.
        assert post_duration == transform_duration
        # pre_tool_call does NOT get duration_ms (nothing has run yet).
        assert "duration_ms" not in kwargs_by_hook["pre_tool_call"]


# =========================================================================
# Agent loop tools
# =========================================================================

class TestAgentLoopTools:
    def test_expected_tools_in_set(self):
        assert "todo" in _AGENT_LOOP_TOOLS
        assert "memory" in _AGENT_LOOP_TOOLS
        assert "session_search" in _AGENT_LOOP_TOOLS
        assert "delegate_task" in _AGENT_LOOP_TOOLS

    def test_no_regular_tools_in_set(self):
        assert "web_search" not in _AGENT_LOOP_TOOLS
        assert "terminal" not in _AGENT_LOOP_TOOLS


# =========================================================================
# Pre-tool-call blocking via plugin hooks
# =========================================================================

class TestPreToolCallBlocking:
    """Verify that pre_tool_call hooks can block tool execution."""

    def test_blocked_tool_returns_error_and_skips_dispatch(self, monkeypatch):
        def fake_invoke_hook(hook_name, **kwargs):
            if hook_name == "pre_tool_call":
                return [{"action": "block", "message": "Blocked by policy"}]
            return []

        dispatch_called = False
        _orig_dispatch = None

        def fake_dispatch(*args, **kwargs):
            nonlocal dispatch_called
            dispatch_called = True
            raise AssertionError("dispatch should not run when blocked")

        monkeypatch.setattr("hermes_cli.plugins.invoke_hook", fake_invoke_hook)
        monkeypatch.setattr("model_tools.registry.dispatch", fake_dispatch)

        result = json.loads(handle_function_call("read_file", {"path": "test.txt"}, task_id="t1"))
        assert result == {"error": "Blocked by policy"}
        assert not dispatch_called

    def test_blocked_tool_skips_read_loop_notification(self, monkeypatch):
        notifications = []

        def fake_invoke_hook(hook_name, **kwargs):
            if hook_name == "pre_tool_call":
                return [{"action": "block", "message": "Blocked"}]
            return []

        monkeypatch.setattr("hermes_cli.plugins.invoke_hook", fake_invoke_hook)
        monkeypatch.setattr("model_tools.registry.dispatch",
                            lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not run")))
        monkeypatch.setattr("tools.file_tools.notify_other_tool_call",
                            lambda task_id: notifications.append(task_id))

        result = json.loads(handle_function_call("web_search", {"q": "test"}, task_id="t1"))
        assert result == {"error": "Blocked"}
        assert notifications == []

    def test_invalid_hook_returns_do_not_block(self, monkeypatch):
        """Malformed hook returns should be ignored — tool executes normally."""
        def fake_invoke_hook(hook_name, **kwargs):
            if hook_name == "pre_tool_call":
                return [
                    "block",
                    {"action": "block"},           # missing message
                    {"action": "deny", "message": "nope"},
                ]
            return []

        monkeypatch.setattr("hermes_cli.plugins.invoke_hook", fake_invoke_hook)
        monkeypatch.setattr("model_tools.registry.dispatch",
                            lambda *a, **kw: json.dumps({"ok": True}))

        result = json.loads(handle_function_call("read_file", {"path": "test.txt"}, task_id="t1"))
        assert result == {"ok": True}

    def test_skip_flag_prevents_double_fire(self, monkeypatch):
        """When skip_pre_tool_call_hook=True, the hook does not fire again.

        The caller (e.g. run_agent._invoke_tool) has already called
        get_pre_tool_call_block_message(), which fires the hook once.
        handle_function_call must NOT fire it a second time — that was
        the classic double-fire bug where observer hooks logged every
        tool call twice.
        """
        hook_calls = []

        def fake_invoke_hook(hook_name, **kwargs):
            hook_calls.append(hook_name)
            return []

        monkeypatch.setattr("hermes_cli.plugins.invoke_hook", fake_invoke_hook)
        monkeypatch.setattr("model_tools.registry.dispatch",
                            lambda *a, **kw: json.dumps({"ok": True}))

        handle_function_call("web_search", {"q": "test"}, task_id="t1",
                             skip_pre_tool_call_hook=True)

        # Single-fire contract: when skip=True the caller already fired
        # pre_tool_call, so handle_function_call must not fire it again.
        assert hook_calls.count("pre_tool_call") == 0, (
            f"pre_tool_call fired {hook_calls.count('pre_tool_call')} times "
            f"with skip_pre_tool_call_hook=True; expected 0 "
            f"(caller already fired it). hook_calls={hook_calls}"
        )
        # post_tool_call and transform_tool_result still fire — only the
        # pre-call block-check path is suppressed by the skip flag.
        assert "post_tool_call" in hook_calls
        assert "transform_tool_result" in hook_calls

    def test_run_agent_pattern_fires_pre_tool_call_exactly_once(self, monkeypatch):
        """End-to-end regression for the double-fire bug.

        Mirrors run_agent._invoke_tool: first calls
        get_pre_tool_call_block_message() (which fires the hook as part of
        its block-directive poll), then calls
        handle_function_call(skip_pre_tool_call_hook=True).  The plugin
        hook MUST fire exactly once across both calls — not twice as it
        did before the fix (observer plugins were seeing every tool
        execution logged twice).
        """
        from hermes_cli.plugins import get_pre_tool_call_block_message

        hook_calls = []

        def fake_invoke_hook(hook_name, **kwargs):
            hook_calls.append(hook_name)
            return []

        monkeypatch.setattr("hermes_cli.plugins.invoke_hook", fake_invoke_hook)
        monkeypatch.setattr("model_tools.registry.dispatch",
                            lambda *a, **kw: json.dumps({"ok": True}))

        # Step 1: caller checks for a block directive (this fires pre_tool_call once).
        block = get_pre_tool_call_block_message(
            "web_search", {"q": "test"}, task_id="t1",
        )
        assert block is None

        # Step 2: caller dispatches with skip=True so the hook isn't re-fired.
        handle_function_call(
            "web_search", {"q": "test"}, task_id="t1",
            skip_pre_tool_call_hook=True,
        )

        assert hook_calls.count("pre_tool_call") == 1, (
            f"pre_tool_call fired {hook_calls.count('pre_tool_call')} times "
            f"across the run_agent (block-check + dispatch) path; "
            f"expected exactly 1. hook_calls={hook_calls}"
        )


# =========================================================================
# Legacy toolset map
# =========================================================================

class TestLegacyToolsetMap:
    def test_expected_legacy_names(self):
        expected = [
            "web_tools", "terminal_tools", "vision_tools", "moa_tools",
            "image_tools", "skills_tools", "browser_tools", "cronjob_tools",
            "file_tools", "tts_tools",
        ]
        for name in expected:
            assert name in _LEGACY_TOOLSET_MAP, f"Missing legacy toolset: {name}"

    def test_values_are_lists_of_strings(self):
        for name, tools in _LEGACY_TOOLSET_MAP.items():
            assert isinstance(tools, list), f"{name} is not a list"
            for tool in tools:
                assert isinstance(tool, str), f"{name} contains non-string: {tool}"


# =========================================================================
# Backward-compat wrappers
# =========================================================================

class TestBackwardCompat:
    def test_get_all_tool_names_returns_list(self):
        names = get_all_tool_names()
        assert isinstance(names, list)
        assert len(names) > 0
        # Should contain well-known tools
        assert "web_search" in names
        assert "terminal" in names

    def test_get_toolset_for_tool(self):
        result = get_toolset_for_tool("web_search")
        assert result is not None
        assert isinstance(result, str)

    def test_get_toolset_for_unknown_tool(self):
        result = get_toolset_for_tool("totally_nonexistent_tool")
        assert result is None

    def test_tool_to_toolset_map(self):
        assert isinstance(TOOL_TO_TOOLSET_MAP, dict)
        assert len(TOOL_TO_TOOLSET_MAP) > 0


# =========================================================================
# _coerce_number — inf / nan must fall through to the original string
# (regression: fix: eliminate duplicate checkpoint entries and JSON-unsafe coercion)
# =========================================================================

class TestCoerceNumberInfNan:
    """_coerce_number must honor its documented contract ("Returns original
    string on failure") for inf/nan inputs, because float('inf') and
    float('nan') are not JSON-compliant under strict serialization."""

    def test_inf_returns_original_string(self):
        from model_tools import _coerce_number
        assert _coerce_number("inf") == "inf"

    def test_negative_inf_returns_original_string(self):
        from model_tools import _coerce_number
        assert _coerce_number("-inf") == "-inf"

    def test_nan_returns_original_string(self):
        from model_tools import _coerce_number
        assert _coerce_number("nan") == "nan"

    def test_infinity_spelling_returns_original_string(self):
        from model_tools import _coerce_number
        # Python's float() parses "Infinity" too — still not JSON-safe.
        assert _coerce_number("Infinity") == "Infinity"

    def test_coerced_result_is_strict_json_safe(self):
        """Whatever _coerce_number returns for inf/nan must round-trip
        through strict (allow_nan=False) json.dumps without raising."""
        from model_tools import _coerce_number
        for s in ("inf", "-inf", "nan", "Infinity"):
            result = _coerce_number(s)
            json.dumps({"x": result}, allow_nan=False)  # must not raise

    def test_normal_numbers_still_coerce(self):
        """Guard against over-correction — real numbers still coerce."""
        from model_tools import _coerce_number
        assert _coerce_number("42") == 42
        assert _coerce_number("3.14") == 3.14
        assert _coerce_number("1e3") == 1000


# =========================================================================
# Kanban worker runtime schema filtering
# =========================================================================

class TestKanbanWorkerEffectiveToolSchema:
    KANBAN_MINIMAL_TOOLS = {
        "kanban_show",
        "kanban_complete",
        "kanban_block",
        "kanban_comment",
    }

    def _schema_names(
        self,
        *,
        clear_cache=True,
        enabled_toolsets=None,
        disabled_toolsets=None,
    ):
        import model_tools
        from tools.registry import invalidate_check_fn_cache

        if clear_cache:
            model_tools._clear_tool_defs_cache()
            invalidate_check_fn_cache()
        tools = get_tool_definitions(
            enabled_toolsets=enabled_toolsets or ["hermes-cli"],
            disabled_toolsets=disabled_toolsets,
            quiet_mode=True,
        )
        return {t["function"]["name"] for t in tools if "function" in t}

    def test_worker_effective_toolsets_filter_model_schema(self, monkeypatch, tmp_path):
        """P1.8: dispatcher-provided effective tools must narrow the actual
        model-native schema, not just appear as prompt/env evidence."""
        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_schema")
        monkeypatch.setenv(
            "HERMES_KANBAN_EFFECTIVE_TOOLSETS",
            json.dumps(sorted(self.KANBAN_MINIMAL_TOOLS)),
        )

        names = self._schema_names()

        assert names == self.KANBAN_MINIMAL_TOOLS
        assert "terminal" not in names
        assert "read_file" not in names
        assert "kanban_create" not in names

    def test_worker_effective_toolsets_survive_profile_disabled_kanban_toolset(
        self,
        monkeypatch,
        tmp_path,
    ):
        """Dispatcher-validated worker tools are the narrow allowlist.

        A profile may disable the broad kanban toolset for normal chats, but a
        spawned worker with HERMES_KANBAN_EFFECTIVE_TOOLSETS must still receive
        exactly those concrete Kanban tools. Otherwise reviewer/admin profiles
        can be safe in chat yet unusable when dispatched.
        """
        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_schema_disabled_kanban")
        monkeypatch.setenv(
            "HERMES_KANBAN_EFFECTIVE_TOOLSETS",
            json.dumps(sorted(self.KANBAN_MINIMAL_TOOLS)),
        )

        names = self._schema_names(
            enabled_toolsets=["hermes-cli"],
            disabled_toolsets=["kanban", "terminal", "file", "skills"],
        )

        assert names == self.KANBAN_MINIMAL_TOOLS
        assert "terminal" not in names
        assert "read_file" not in names
        assert "skill_view" not in names

    def test_worker_minimal_schema_excludes_fake_mcp_despite_broad_toolset_context(
        self,
        monkeypatch,
        tmp_path,
    ):
        """P1.10 second slice: prove model-native worker schema filtering is
        stronger than broad context/toolset expansion.

        This is test-only: it registers an in-memory fake MCP-style toolset and
        proves that the broad resolver can see it, then proves a restricted
        Kanban worker sees exactly the Kanban minimal tools and not the fake MCP
        tool.  No dispatcher spawn and no real MCP process/discovery is used.
        """
        from tools.registry import registry

        fake_tool = "mcp_fake_context_probe"
        fake_toolset = "mcp-fake-broad-context"
        fake_alias = "fake-mcp-toolset"
        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        registry.register(
            name=fake_tool,
            toolset=fake_toolset,
            schema={
                "name": fake_tool,
                "description": "Fake MCP tool used only for schema-filter tests.",
                "parameters": {"type": "object", "properties": {}},
            },
            handler=lambda args, **kw: json.dumps({"ok": True}),
            check_fn=lambda: True,
        )
        registry.register_toolset_alias(fake_alias, fake_toolset)
        try:
            broad_names = self._schema_names(enabled_toolsets=["hermes-cli", fake_alias])
            assert fake_tool in broad_names
            assert "terminal" in broad_names
            assert "read_file" in broad_names

            monkeypatch.setenv("HERMES_KANBAN_TASK", "t_schema_fake_mcp_absence")
            monkeypatch.setenv(
                "HERMES_KANBAN_EFFECTIVE_TOOLSETS",
                json.dumps(sorted(self.KANBAN_MINIMAL_TOOLS)),
            )

            worker_names = self._schema_names(enabled_toolsets=["hermes-cli", fake_alias])

            assert worker_names == self.KANBAN_MINIMAL_TOOLS
            assert fake_tool not in worker_names
            assert "terminal" not in worker_names
            assert "read_file" not in worker_names
        finally:
            registry.deregister(fake_tool)
            import model_tools
            from tools.registry import invalidate_check_fn_cache
            model_tools._clear_tool_defs_cache()
            invalidate_check_fn_cache()

    def test_worker_minimal_schema_excludes_delegation_and_fake_mcp_despite_broad_toolset_context(
        self,
        monkeypatch,
        tmp_path,
    ):
        """P1.11: restricted Kanban worker schema excludes delegation/subagent
        tools even when broad context can see delegation and a fake MCP toolset.

        This is test-only: it registers an in-memory fake MCP-style toolset,
        proves the broad resolver can see both delegate_task and the fake MCP
        tool, then proves a restricted Kanban worker sees exactly the Kanban
        minimal tools.  No dispatcher spawn, no subagent spawn, and no real MCP
        process/discovery is used.
        """
        from tools.registry import registry

        fake_tool = "mcp_fake_context_probe"
        fake_toolset = "mcp-fake-broad-context"
        fake_alias = "fake-mcp-toolset"
        broad_toolsets = ["hermes-cli", "delegation", fake_alias]
        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        registry.register(
            name=fake_tool,
            toolset=fake_toolset,
            schema={
                "name": fake_tool,
                "description": "Fake MCP tool used only for schema-filter tests.",
                "parameters": {"type": "object", "properties": {}},
            },
            handler=lambda args, **kw: json.dumps({"ok": True}),
            check_fn=lambda: True,
        )
        registry.register_toolset_alias(fake_alias, fake_toolset)
        try:
            broad_names = self._schema_names(enabled_toolsets=broad_toolsets)
            assert "delegate_task" in broad_names
            assert fake_tool in broad_names
            assert "terminal" in broad_names
            assert "read_file" in broad_names

            monkeypatch.setenv("HERMES_KANBAN_TASK", "t_schema_delegate_absence")
            monkeypatch.setenv(
                "HERMES_KANBAN_EFFECTIVE_TOOLSETS",
                json.dumps(sorted(self.KANBAN_MINIMAL_TOOLS)),
            )

            worker_names = self._schema_names(enabled_toolsets=broad_toolsets)

            assert worker_names == self.KANBAN_MINIMAL_TOOLS
            assert "delegate_task" not in worker_names
            assert fake_tool not in worker_names
            assert "terminal" not in worker_names
            assert "read_file" not in worker_names
            assert "execute_code" not in worker_names
            assert "memory" not in worker_names
            assert "clarify" not in worker_names
            assert "send_message" not in worker_names
        finally:
            registry.deregister(fake_tool)
            import model_tools
            from tools.registry import invalidate_check_fn_cache
            model_tools._clear_tool_defs_cache()
            invalidate_check_fn_cache()

    def test_worker_runtime_denies_synthetic_forbidden_calls_before_handlers(
        self,
        monkeypatch,
        tmp_path,
    ):
        """P1.17: restricted Kanban workers fail closed at runtime too.

        Schema absence is not enough: if a provider/test/malicious worker sends
        a forbidden synthetic tool call anyway, the dispatcher must deny it
        before any registry handler, agent-loop tool path, plugin hook, or
        read-loop side effect runs.  This is test-only and uses fake handlers;
        no real terminal/file/process/delegation/MCP handlers are invoked.
        """
        from tools.registry import registry

        home = tmp_path / ".hermes"
        home.mkdir()
        allowed = sorted(self.KANBAN_MINIMAL_TOOLS)
        forbidden_tools = [
            "terminal",
            "process",
            "read_file",
            "write_file",
            "patch",
            "delegate_task",
            "memory",
            "skill_manage",
            "mcp_fake_context_probe",
        ]
        fake_tool = "mcp_fake_context_probe"
        fake_toolset = "mcp-fake-runtime-denial"
        handler_calls = {name: 0 for name in forbidden_tools + ["kanban_show"]}
        plugin_calls = []
        read_loop_notifications = []

        def fake_dispatch(name, args, **kwargs):
            handler_calls[name] = handler_calls.get(name, 0) + 1
            return json.dumps({"ok": True, "tool": name})

        def fake_hook(hook_name, **kwargs):
            plugin_calls.append((hook_name, kwargs.get("tool_name")))
            return []

        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_runtime_denial")
        monkeypatch.setenv("HERMES_KANBAN_EFFECTIVE_TOOLSETS", json.dumps(allowed))
        monkeypatch.setattr("model_tools.registry.dispatch", fake_dispatch)
        monkeypatch.setattr("hermes_cli.plugins.invoke_hook", fake_hook)
        monkeypatch.setattr("tools.file_tools.notify_other_tool_call", lambda task_id: read_loop_notifications.append(task_id))
        registry.register(
            name=fake_tool,
            toolset=fake_toolset,
            schema={
                "name": fake_tool,
                "description": "Fake MCP runtime-denial probe.",
                "parameters": {"type": "object", "properties": {}},
            },
            handler=lambda args, **kw: (_ for _ in ()).throw(AssertionError("fake MCP handler must not run")),
            check_fn=lambda: True,
        )
        try:
            for tool_name in forbidden_tools:
                result = json.loads(handle_function_call(tool_name, {"probe": True}, task_id="t-runtime"))
                assert result["event"] == "kanban_worker_tool_call_denied"
                assert result["tool_name"] == tool_name
                assert result["kanban_task_context"] is True
                assert result["effective_tool_filter_active"] is True
                assert "effective_toolsets" in result["error"]
                assert "t_runtime_denial" not in json.dumps(result)

            assert all(handler_calls[name] == 0 for name in forbidden_tools)
            assert plugin_calls == []
            assert read_loop_notifications == []

            allowed_result = json.loads(handle_function_call("kanban_show", {}, task_id="t-runtime"))
            assert allowed_result == {"ok": True, "tool": "kanban_show"}
            assert handler_calls["kanban_show"] == 1
        finally:
            registry.deregister(fake_tool)
            import model_tools
            from tools.registry import invalidate_check_fn_cache
            model_tools._clear_tool_defs_cache()
            invalidate_check_fn_cache()

    def test_worker_effective_toolsets_cache_key_is_env_sensitive(self, monkeypatch, tmp_path):
        """Quiet-mode schema cache must not leak one worker's restricted
        schema into another worker with a different effective list."""
        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_schema")
        monkeypatch.setenv("HERMES_KANBAN_EFFECTIVE_TOOLSETS", json.dumps(["kanban_show"]))
        first_names = self._schema_names()

        monkeypatch.setenv("HERMES_KANBAN_EFFECTIVE_TOOLSETS", json.dumps(["kanban_block"]))
        second_names = self._schema_names(clear_cache=False)

        assert "kanban_show" in first_names
        assert "kanban_block" not in first_names
        assert "kanban_block" in second_names
        assert "kanban_show" not in second_names

    def test_invalid_worker_effective_toolsets_fails_closed(self, monkeypatch, tmp_path):
        """Malformed dispatcher env must not fall back to the broad worker
        schema; fail closed instead of leaking unlisted tools."""
        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_schema")
        monkeypatch.setenv("HERMES_KANBAN_EFFECTIVE_TOOLSETS", "not-json")

        names = self._schema_names()

        assert names == set()

    def test_schema_audit_env_emits_safe_tool_name_snapshot(self, monkeypatch, tmp_path, capsys):
        """P1.9 step 1: opt-in audit output must expose only safe schema
        metadata so a later real worker run can prove forbidden tools absent."""
        home = tmp_path / ".hermes"
        home.mkdir()
        effective = json.dumps(["kanban_show", "kanban_complete"])
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_schema_secret_not_logged")
        monkeypatch.setenv("HERMES_KANBAN_EFFECTIVE_TOOLSETS", effective)
        monkeypatch.setenv("HERMES_KANBAN_SCHEMA_AUDIT", "1")

        names = self._schema_names()
        stderr = capsys.readouterr().err

        assert names == {"kanban_show", "kanban_complete"}
        audit_line = next(
            line for line in stderr.splitlines()
            if line.startswith("HERMES_KANBAN_SCHEMA_AUDIT ")
        )
        audit = json.loads(audit_line.split(" ", 1)[1])
        assert audit == {
            "event": "kanban_worker_tool_schema",
            "kanban_task_context": True,
            "effective_tool_filter_active": True,
            "tool_count": 2,
            "tool_names": ["kanban_complete", "kanban_show"],
        }
        assert effective not in stderr
        assert "t_schema_secret_not_logged" not in stderr
        assert "terminal" not in stderr
        assert "read_file" not in stderr

    def test_schema_audit_env_absent_is_silent(self, monkeypatch, tmp_path, capsys):
        """Normal schema generation must not emit audit lines unless the
        explicit audit env gate is enabled."""
        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_schema")
        monkeypatch.setenv("HERMES_KANBAN_EFFECTIVE_TOOLSETS", json.dumps(["kanban_show"]))
        monkeypatch.delenv("HERMES_KANBAN_SCHEMA_AUDIT", raising=False)

        assert self._schema_names() == {"kanban_show"}
        assert "HERMES_KANBAN_SCHEMA_AUDIT" not in capsys.readouterr().err
